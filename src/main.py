"""
Точка входа My Claude Bot.

Загружает .env, находит всех агентов (agents/*/agent.yaml),
создаёт Telegram-ботов, MessageBus, Orchestrator, Dream и Heartbeat.
Запускает всё в одном asyncio loop.

FleetRuntime — глобальный контекст для hot-reload агентов.
"""

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import memory
from .agent import Agent
from .agent_manager import AgentManager
from .agent_worker import AgentWorker
from .bus import FleetBus
from .cron import cron_loop
from .delegation import DelegationManager
from .dream import dream_loop
from .heartbeat import heartbeat_loop
from .orchestrator import Orchestrator
from .telegram_bridge import TelegramBridge

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("my-claude-bot")

# Максимум параллельных Claude CLI процессов
MAX_CONCURRENT_CLAUDE = 3


def find_project_root() -> Path:
    """Найти корень проекта (где лежит agents/)."""
    # Пробуем от текущей директории вверх
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "agents").is_dir():
            return parent
    # Fallback: директория рядом с src/
    src_dir = Path(__file__).parent
    return src_dir.parent


def load_agents(root: Path) -> list[Agent]:
    """Найти и загрузить всех агентов из agents/*/agent.yaml."""
    agents_dir = root / "agents"
    if not agents_dir.exists():
        logger.error(f"Директория agents/ не найдена в {root}")
        return []

    agents = []
    for agent_yaml in sorted(agents_dir.glob("*/agent.yaml")):
        try:
            agent = Agent(str(agent_yaml))

            # Пропустить агента если bot_token не задан в .env
            if not agent.bot_token or "${" in agent.bot_token:
                logger.warning(
                    f"Агент '{agent.name}' пропущен: "
                    f"bot_token не задан (добавь в .env)"
                )
                continue

            agents.append(agent)
            logger.info(f"Загружен агент: {agent.name} ({agent.display_name})")
        except Exception as e:
            logger.error(f"Ошибка загрузки {agent_yaml}: {e}")

    return agents


class FleetRuntime:
    """
    Глобальный контекст для управления агентами на лету.

    Позволяет запускать и останавливать агентов без перезапуска платформы.
    """

    def __init__(
        self,
        root: Path,
        bus: FleetBus,
        semaphore: asyncio.Semaphore,
        orchestrator: Orchestrator,
    ):
        self.root = root
        self.bus = bus
        self.semaphore = semaphore
        self.orchestrator = orchestrator
        self.manager = AgentManager(root)

        # Состояние запущенных агентов
        self.agents: dict[str, Agent] = {}
        self.workers: dict[str, AgentWorker] = {}
        self.bridges: dict[str, TelegramBridge] = {}
        self.tasks: dict[str, list[asyncio.Task]] = {}

    def register_running(
        self,
        agent: Agent,
        worker: AgentWorker,
        bridge: TelegramBridge,
        agent_tasks: list[asyncio.Task],
    ) -> None:
        """Зарегистрировать уже запущенного агента."""
        self.agents[agent.name] = agent
        self.workers[agent.name] = worker
        self.bridges[agent.name] = bridge
        self.tasks[agent.name] = agent_tasks

    def is_running(self, name: str) -> bool:
        """Проверить, запущен ли агент."""
        return name in self.tasks and any(
            not t.done() for t in self.tasks[name]
        )

    def running_agents(self) -> list[str]:
        """Список имён запущенных агентов."""
        return [n for n in self.tasks if self.is_running(n)]

    async def start_agent(self, name: str) -> tuple[bool, str]:
        """
        Запустить агента по имени.

        Returns:
            (ok, message)
        """
        if self.is_running(name):
            return False, f"Агент '{name}' уже запущен"

        agent_yaml = self.root / "agents" / name / "agent.yaml"
        if not agent_yaml.exists():
            return False, f"Агент '{name}' не найден"

        # Перезагрузить .env чтобы подхватить новые токены
        env_file = self.root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=True)

        try:
            agent = Agent(str(agent_yaml))
        except Exception as e:
            return False, f"Ошибка загрузки агента: {e}"

        if not agent.bot_token or "${" in agent.bot_token:
            return False, f"Токен агента '{name}' не задан в .env"

        # Git memory
        if memory.git_init(agent.agent_dir):
            logger.info(f"Git memory initialized for '{name}'")

        # Bus
        self.bus.subscribe(f"agent:{name}")
        self.bus.subscribe(f"telegram:{name}")

        # Orchestrator — обновить agents_dict
        self.orchestrator.agents[name] = agent

        # Worker
        worker = AgentWorker(agent, self.bus, self.semaphore)
        worker_task = asyncio.create_task(worker.run())

        # Bridge
        bridge = TelegramBridge(
            agent, self.semaphore, bus=self.bus, agent_worker=worker
        )
        bot_task = asyncio.create_task(run_bot(bridge))

        agent_tasks = [worker_task, bot_task]

        # Delegation (только для master)
        if agent.is_master:
            delegation = DelegationManager(agent.name, agent.agent_dir, self.bus)
            delegation_task = asyncio.create_task(delegation.watch())
            agent_tasks.append(delegation_task)

        # Dream
        dream_config = agent.config.get("dream", {})
        if dream_config:
            interval = dream_config.get("interval_hours", 2.0)
            model_p1 = dream_config.get("model_phase1", "haiku")
            model_p2 = dream_config.get("model_phase2", "sonnet")
            dream_task = asyncio.create_task(
                dream_loop(agent.agent_dir, interval, model_p1, model_p2)
            )
            agent_tasks.append(dream_task)

        # Heartbeat
        hb_config = agent.config.get("heartbeat", {})
        if hb_config.get("enabled", False):
            interval = hb_config.get("interval_minutes", 30.0)
            hb_task = asyncio.create_task(
                heartbeat_loop(
                    agent.agent_dir, agent.name,
                    bus=self.bus, interval_minutes=interval,
                )
            )
            agent_tasks.append(hb_task)

        # Cron
        if agent.config.get("cron"):
            cron_task = asyncio.create_task(
                cron_loop(
                    agent.config, agent.agent_dir, agent.name, bus=self.bus,
                )
            )
            agent_tasks.append(cron_task)

        self.register_running(agent, worker, bridge, agent_tasks)
        logger.info(f"Агент '{name}' запущен (hot-reload)")
        return True, f"Агент '{name}' запущен"

    async def stop_agent(self, name: str) -> tuple[bool, str]:
        """
        Остановить агента по имени.

        Returns:
            (ok, message)
        """
        if not self.is_running(name):
            return False, f"Агент '{name}' не запущен"

        # Отменить все задачи
        for task in self.tasks.get(name, []):
            if not task.done():
                task.cancel()

        # Подождать завершения
        for task in self.tasks.get(name, []):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Отписаться от bus
        self.bus.unsubscribe(f"agent:{name}")
        self.bus.unsubscribe(f"telegram:{name}")

        # Удалить из orchestrator
        self.orchestrator.agents.pop(name, None)

        # Очистить
        self.agents.pop(name, None)
        self.workers.pop(name, None)
        self.bridges.pop(name, None)
        self.tasks.pop(name, None)

        logger.info(f"Агент '{name}' остановлен (hot-reload)")
        return True, f"Агент '{name}' остановлен"


async def run_bot(bridge: TelegramBridge) -> None:
    """Запустить один Telegram-бот."""
    app = bridge.build_app()
    bus_listener_task = None
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info(f"Бот '{bridge.agent.name}' запущен")

        # Запустить bus listener (если bus подключён)
        if bridge.bus:
            bus_listener_task = asyncio.create_task(
                bridge.start_bus_listener(app)
            )

        # Ждём бесконечно (до отмены)
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info(f"Бот '{bridge.agent.name}' останавливается...")
        if bus_listener_task:
            bus_listener_task.cancel()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def async_main() -> None:
    """Главная async функция."""
    root = find_project_root()
    logger.info(f"Корень проекта: {root}")

    # Загрузить .env
    env_file = root / ".env"
    if env_file.exists():
        load_dotenv(env_file)
        logger.info("Загружен .env")
    else:
        logger.warning(
            f".env не найден в {root}. "
            "Скопируй .env.example → .env и заполни токены."
        )

    # Загрузить агентов
    agents = load_agents(root)
    if not agents:
        logger.error("Нет агентов для запуска. Проверь agents/*/agent.yaml")
        sys.exit(1)

    # Глобальный семафор для Claude CLI
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLAUDE)

    # Инициализировать git для памяти каждого агента
    for agent in agents:
        if memory.git_init(agent.agent_dir):
            logger.info(f"Git memory initialized for '{agent.name}'")

    # ── MessageBus ──
    bus = FleetBus()
    agents_dict = {a.name: a for a in agents}

    # ── Orchestrator ──
    orchestrator = Orchestrator(bus, agents_dict)

    # Подписать каждого агента на шину
    for agent in agents:
        bus.subscribe(f"agent:{agent.name}")

    # ── FleetRuntime для hot-reload ──
    runtime = FleetRuntime(root, bus, semaphore, orchestrator)

    tasks = []

    # Запустить Orchestrator
    tasks.append(asyncio.create_task(orchestrator.run()))
    logger.info("Orchestrator запущен")

    # ── Agent Workers + Telegram bots ──
    workers: dict[str, AgentWorker] = {}
    for agent in agents:
        # Worker
        worker = AgentWorker(agent, bus, semaphore)
        workers[agent.name] = worker
        worker_task = asyncio.create_task(worker.run())
        tasks.append(worker_task)
        logger.info(f"AgentWorker '{agent.name}' запущен")

        # Bridge
        bridge = TelegramBridge(
            agent, semaphore, bus=bus, agent_worker=worker,
            fleet_runtime=runtime,
        )
        bus.subscribe(f"telegram:{agent.name}")
        bot_task = asyncio.create_task(run_bot(bridge))
        tasks.append(bot_task)

        # Регистрация в runtime (worker + bot tasks)
        runtime.register_running(agent, worker, bridge, [worker_task, bot_task])
        logger.info(f"Бот '{agent.name}' добавлен в очередь запуска")

    # ── Delegation Managers (только для master-агентов) ──
    for agent in agents:
        if not agent.is_master:
            continue
        delegation = DelegationManager(agent.name, agent.agent_dir, bus)
        delegation_task = asyncio.create_task(delegation.watch())
        if agent.name in runtime.tasks:
            runtime.tasks[agent.name].append(delegation_task)
        tasks.append(delegation_task)
        logger.info(f"DelegationManager запущен для master '{agent.name}'")

    # ── Dream Memory ──
    for agent in agents:
        dream_config = agent.config.get("dream", {})
        if dream_config:
            interval = dream_config.get("interval_hours", 2.0)
            model_p1 = dream_config.get("model_phase1", "haiku")
            model_p2 = dream_config.get("model_phase2", "sonnet")
            dream_task = asyncio.create_task(
                dream_loop(agent.agent_dir, interval, model_p1, model_p2)
            )
            # Добавить в runtime
            if agent.name in runtime.tasks:
                runtime.tasks[agent.name].append(dream_task)
            tasks.append(dream_task)
            logger.info(f"Dream loop запущен для '{agent.name}' (каждые {interval}ч)")

    # ── Heartbeat ──
    for agent in agents:
        hb_config = agent.config.get("heartbeat", {})
        if hb_config.get("enabled", False):
            interval = hb_config.get("interval_minutes", 30.0)
            hb_task = asyncio.create_task(
                heartbeat_loop(
                    agent.agent_dir,
                    agent.name,
                    bus=bus,
                    interval_minutes=interval,
                )
            )
            if agent.name in runtime.tasks:
                runtime.tasks[agent.name].append(hb_task)
            tasks.append(hb_task)
            logger.info(f"Heartbeat запущен для '{agent.name}' (каждые {interval} мин)")

    # ── Cron ──
    for agent in agents:
        if agent.config.get("cron"):
            cron_task = asyncio.create_task(
                cron_loop(
                    agent.config,
                    agent.agent_dir,
                    agent.name,
                    bus=bus,
                )
            )
            if agent.name in runtime.tasks:
                runtime.tasks[agent.name].append(cron_task)
            tasks.append(cron_task)
            cron_names = [j["name"] for j in agent.config["cron"]]
            logger.info(f"Cron запущен для '{agent.name}': {', '.join(cron_names)}")

    logger.info(
        f"Fleet запущен: {len(agents)} агентов, "
        f"{len(tasks)} задач. Ctrl+C для остановки."
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        orchestrator.stop()


def main() -> None:
    """Точка входа (синхронная)."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")


if __name__ == "__main__":
    main()
