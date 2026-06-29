"""
Точка входа My Claude Bot.

Загружает .env, находит всех агентов (agents/*/agent.yaml),
создаёт Telegram-ботов, MessageBus, Orchestrator, Dream и Heartbeat.
Запускает всё в одном asyncio loop.

FleetRuntime — глобальный контекст для hot-reload агентов.
"""

import asyncio
import fcntl
import hashlib
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Held for process lifetime so the flock isn't released by GC.
_singleton_lock_handle = None


def _master_notify_chat_id(agent: "Agent") -> int:
    """Telegram chat_id for master-agent background notifications.

    For DM the user_id equals the chat_id, so FOUNDER_TELEGRAM_ID is the
    right target for SmartHeartbeat/Legacy heartbeat pushes. Workers don't
    have a founder concept, so fall back to 0 (no push).
    """
    if not agent.is_master:
        return 0
    try:
        return int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
    except ValueError:
        return 0


def _founder_chat_id(root: "Path | None" = None) -> int:
    """Return FOUNDER_TELEGRAM_ID as int, or 0 if not set/invalid.

    Fallback chain (stops at first non-zero result):
      1. FOUNDER_TELEGRAM_ID env var
      2. agents/me/memory/settings.json  — keys: telegram_id / chat_id
      3. agents/me/memory/profile.md     — regex: telegram[_ ]id: <digits>
      4. 0 + warning (misconfigured deployment)

    Used for background notifications that must reach the founder regardless
    of which agent (master or worker) generated them.
    """
    import json
    import re

    # 1. Env var (canonical config)
    try:
        val = int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
        if val:
            return val
    except ValueError:
        pass

    # 2-3. Fallback: read from master-agent memory
    if root is None:
        root = find_project_root()

    # settings.json
    settings_path = root / "agents" / "me" / "memory" / "settings.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            for key in ("telegram_id", "chat_id", "founder_telegram_id"):
                raw = data.get(key)
                if raw:
                    val = int(raw)
                    if val:
                        logger.info(
                            f"_founder_chat_id: получен из settings.json "
                            f"({key}={val})"
                        )
                        return val
        except Exception:
            pass

    # profile.md
    profile_path = root / "agents" / "me" / "memory" / "profile.md"
    if profile_path.exists():
        try:
            text = profile_path.read_text(encoding="utf-8")
            for pattern in (
                r"telegram[_\s]id[:\s*]+(\d{5,})",
                r"chat[_\s]id[:\s*]+(\d{5,})",
                r"FOUNDER_TELEGRAM_ID[:\s*]+(\d{5,})",
            ):
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    val = int(m.group(1))
                    if val:
                        logger.info(
                            f"_founder_chat_id: получен из profile.md (={val})"
                        )
                        return val
        except Exception:
            pass

    logger.warning(
        "FOUNDER_TELEGRAM_ID не задан в .env и не найден в agents/me/memory. "
        "Уведомления от фоновых задач (cron, reminders) не будут доставлены. "
        "Добавьте FOUNDER_TELEGRAM_ID=<ваш_telegram_id> в .env"
    )
    return 0

from . import memory
from .agent import Agent
from .agent_manager import AgentManager
from .agent_worker import AgentWorker
from .bus import FleetBus
from .cron import cron_loop
from .reminders import reminders_loop
from .delegation import DelegationManager
from .dispatcher import dispatcher_loop
from .dream import dream_loop
from .github_sync import github_sync_loop
from .http_server import serve_forever as http_serve_forever
from .knowledge_graph import nightly_graph_loop
from .heartbeat import heartbeat_loop
from .orchestrator import Orchestrator
from .skill_advisor import (
    SkillSuggestionReceiver,
    run_daily_digest,
    skill_digest_loop,
)
from .smart_heartbeat import SmartHeartbeat
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
        # MAX-мосты (опциональный второй транспорт). Ключ — имя агента.
        # Тип намеренно loose: maxapi — опциональная зависимость, не импортим
        # MaxBridge на уровне модуля, чтобы флот поднимался и без неё.
        self.max_bridges: dict = {}
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

        # Bridge (Telegram)
        bridge = TelegramBridge(
            agent, self.semaphore, bus=self.bus, agent_worker=worker
        )
        bot_task = asyncio.create_task(run_bot(bridge))

        agent_tasks = [worker_task, bot_task]

        # MAX bridge (опциональный второй транспорт, если задан max_bot_token)
        max_task = _maybe_start_max_bridge(
            agent, self.semaphore, self.bus, worker, self
        )
        if max_task:
            agent_tasks.append(max_task)

        # Delegation (только для master)
        if agent.is_master:
            delegation = DelegationManager(agent.name, agent.agent_dir, self.bus)
            delegation_task = asyncio.create_task(delegation.watch())
            agent_tasks.append(delegation_task)

            # SkillSuggestionReceiver (master принимает предложения)
            receiver = SkillSuggestionReceiver(
                agent.agent_dir, agent.name, self.bus
            )
            receiver_task = asyncio.create_task(receiver.run())
            agent_tasks.append(receiver_task)

            # SkillDigest loop (ежедневная сводка владельцу)
            sa_master_config = agent.config.get("skill_advisor_digest", {})
            digest_hour = sa_master_config.get("hour", 20)
            digest_task = asyncio.create_task(
                skill_digest_loop(
                    agent.agent_dir, agent.name, self.bus,
                    check_hour=digest_hour,
                )
            )
            agent_tasks.append(digest_task)

        # Dream
        dream_config = agent.config.get("dream", {})
        if dream_config:
            interval = dream_config.get("interval_hours", 2.0)
            model_p1 = dream_config.get("model_phase1", "haiku")
            model_p2 = dream_config.get("model_phase2", "sonnet")
            sa_config = agent.config.get("skill_advisor")
            schema_adv_config = agent.config.get("schema_advisor")
            dream_task = asyncio.create_task(
                dream_loop(
                    agent.agent_dir, interval, model_p1, model_p2,
                    skill_advisor_config=sa_config,
                    schema_advisor_config=schema_adv_config,
                    bus=self.bus,
                    agent_name=agent.name,
                )
            )
            agent_tasks.append(dream_task)

        # Heartbeat (smart или legacy)
        hb_config = agent.config.get("heartbeat", {})
        if hb_config.get("enabled", False):
            if hb_config.get("triggers"):
                # Smart heartbeat с триггерами
                smart_hb = SmartHeartbeat(
                    agent.agent_dir, agent.name, hb_config, bus=self.bus,
                    chat_id=_master_notify_chat_id(agent),
                )
                hb_task = asyncio.create_task(smart_hb.run())
            else:
                # Legacy heartbeat
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
            # Worker-агенты не имеют прямого чата с пользователем (chat_id=0).
            # Уведомления маршрутизируем через мастер-агента: находим его в
            # self.agents и передаём имя как notify_agent_name.
            if agent.is_master:
                cron_chat_id = _master_notify_chat_id(agent)
                cron_notify_agent = None
            else:
                cron_chat_id = _founder_chat_id(self.root)
                cron_notify_agent = next(
                    (n for n, a in self.agents.items() if a.is_master), None
                )
            cron_task = asyncio.create_task(
                cron_loop(
                    agent.config, agent.agent_dir, agent.name, bus=self.bus,
                    chat_id=cron_chat_id,
                    notify_agent_name=cron_notify_agent,
                )
            )
            agent_tasks.append(cron_task)

        # Reminders persistence loop — восстанавливает CronCreate-напоминания
        # после перезапуска. Запускается для всех агентов безусловно:
        # reminders.json может появиться в любой момент.
        # Worker-агенты: используем founder chat_id (аналогично cron_loop).
        reminders_chat_id = (
            _master_notify_chat_id(agent)
            if agent.is_master
            else _founder_chat_id(self.root)
        )
        reminders_task = asyncio.create_task(
            reminders_loop(
                agent.agent_dir, agent.name, bus=self.bus,
                chat_id=reminders_chat_id,
            )
        )
        agent_tasks.append(reminders_task)

        # Dispatcher: поллит memory/dispatch/ и публикует в bus.
        # Нужен для cron/heartbeat/self-triggered сообщений, которые
        # агент адресует в конкретный chat_id+thread_id.
        dispatch_task = asyncio.create_task(
            dispatcher_loop(agent.agent_dir, agent.name, bus=self.bus)
        )
        agent_tasks.append(dispatch_task)

        # Knowledge Graph (ночной пайплайн связей)
        kg_config = agent.config.get("knowledge_graph", {})
        if kg_config.get("enabled", False):
            kg_task = asyncio.create_task(
                nightly_graph_loop(
                    agent.agent_dir,
                    config=kg_config,
                    run_hour=kg_config.get("run_hour", 1),
                    run_minute=kg_config.get("run_minute", 0),
                )
            )
            agent_tasks.append(kg_task)

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

        # Отписаться от bus (unsubscribe идемпотентен — max: no-op, если не было)
        self.bus.unsubscribe(f"agent:{name}")
        self.bus.unsubscribe(f"telegram:{name}")
        self.bus.unsubscribe(f"max:{name}")

        # Удалить из orchestrator
        self.orchestrator.agents.pop(name, None)

        # Очистить
        self.agents.pop(name, None)
        self.workers.pop(name, None)
        self.bridges.pop(name, None)
        self.max_bridges.pop(name, None)
        self.tasks.pop(name, None)

        logger.info(f"Агент '{name}' остановлен (hot-reload)")
        return True, f"Агент '{name}' остановлен"


async def run_bot(bridge: TelegramBridge) -> None:
    """Запустить один Telegram-бот."""
    app = bridge.build_app()
    bus_listener_task = None
    try:
        await app.initialize()
        # post_init не вызывается автоматически при ручном initialize(),
        # поэтому вызываем явно (регистрация команд + git init)
        if app.post_init:
            await app.post_init(app)
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
        # Каждый шаг shutdown — под своим try/except. Если updater не успел
        # стартовать (падение на app.start()/start_polling), app.updater.stop()
        # кидает RuntimeError("This Updater is not running!"), который раньше
        # затирал исходную ошибку И ронял остальные шаги (утечка app.stop/
        # shutdown). Теперь каждый шаг изолирован — graceful degrade.
        for _close, _label in (
            (app.updater.stop, "updater.stop"),
            (app.stop, "stop"),
            (app.shutdown, "shutdown"),
        ):
            try:
                await _close()
            except Exception as _e:
                logger.debug(
                    f"Бот '{bridge.agent.name}' {_label} на shutdown: {_e}"
                )


def _maybe_start_max_bridge(
    agent: Agent,
    semaphore: asyncio.Semaphore,
    bus: FleetBus,
    worker: AgentWorker,
    runtime: "FleetRuntime",
) -> asyncio.Task | None:
    """Поднять MAX-bridge для агента, если у него настроен max_bot_token.

    Возвращает task запущенного бота или None (если транспорт не настроен или
    maxapi не установлен). maxapi — опциональная зависимость: при её отсутствии
    деградируем gracefully (агент остаётся только в Telegram), по аналогии с
    bubblewrap для sandbox. Подписка на `max:{name}` и регистрация bridge в
    runtime — здесь же, чтобы обе точки старта (boot + hot-reload) были единообразны.
    """
    if not agent.max_bot_token:
        return None
    try:
        from .max_bridge import MaxBridge
    except ImportError as e:
        logger.warning(
            f"MAX-bridge '{agent.name}' пропущен: maxapi не установлен ({e}). "
            "Установи зависимость (`pip install maxapi`), чтобы включить МАКС."
        )
        return None
    try:
        max_bridge = MaxBridge(
            agent, semaphore, bus=bus, agent_worker=worker,
            fleet_runtime=runtime,
        )
    except Exception as e:
        logger.error(f"MAX-bridge '{agent.name}' не создан: {e}")
        return None
    bus.subscribe(f"max:{agent.name}")
    task = asyncio.create_task(max_bridge.run())
    runtime.max_bridges[agent.name] = max_bridge
    logger.info(f"MAX-бот '{agent.name}' запущен")
    return task


def _cleanup_qmd() -> None:
    """Удалить qmd и его кэш, если остались от предыдущих версий."""
    import shutil

    home = Path.home()
    qmd_bin = home / ".local" / "bin" / "qmd"
    qmd_lib = home / ".local" / "lib" / "node_modules" / "@tobilu" / "qmd"
    qmd_cache = home / ".cache" / "qmd"

    cleaned = False
    for path in [qmd_bin, qmd_lib, qmd_cache]:
        if path.exists():
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                logger.info(f"qmd cleanup: удалён {path}")
                cleaned = True
            except OSError as e:
                logger.warning(f"qmd cleanup: не удалось удалить {path}: {e}")

    # Почистить пустую директорию @tobilu если осталась
    tobilu_dir = home / ".local" / "lib" / "node_modules" / "@tobilu"
    if tobilu_dir.exists() and not any(tobilu_dir.iterdir()):
        try:
            tobilu_dir.rmdir()
        except OSError:
            pass

    if cleaned:
        logger.info("qmd удалён — wiki-поиск работает через встроенный search_wiki()")


def _acquire_singleton_lock(agents: list[Agent]) -> None:
    """Prevent a second instance from polling Telegram with the same tokens.

    Two processes calling getUpdates for the same bot token make Telegram
    kick both with `Conflict`. The lock file lives at `/tmp/my-claude-bot-
    <sha256-of-tokens>.lock` so it catches any double-start combo —
    systemd+docker, two systemd units, `docker compose up` on top of a
    running service, a forgotten `nohup`. Mode 0666 lets a container
    running under a different host UID see the same lock.
    """
    global _singleton_lock_handle

    tokens = sorted({a.bot_token for a in agents if a.bot_token and "${" not in a.bot_token})
    if not tokens:
        return

    digest = hashlib.sha256("|".join(tokens).encode()).hexdigest()[:16]
    lock_path = Path(f"/tmp/my-claude-bot-{digest}.lock")

    prev_umask = os.umask(0)
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
    finally:
        os.umask(prev_umask)

    fh = os.fdopen(fd, "r+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        existing = fh.read().strip() or "unknown"
        fh.close()
        logger.error(
            "Another bot instance is already running for these Telegram tokens (pid %s).\n"
            "  Two processes polling the same token = Telegram kicks both with Conflict.\n"
            "  Check:  systemctl --user status my-claude-bot   and   docker ps\n"
            "  Fix:    stop one of them (likely `docker compose down` if systemd is the canonical deploy).",
            existing,
        )
        sys.exit(1)

    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _singleton_lock_handle = fh


def _check_bubblewrap_requirements(agents: list[Agent]) -> None:
    """Проверить, что bwrap установлен если агенты его требуют.

    Если bwrap отсутствует — НЕ падаем, а деградируем до hook-only sandbox:
    у агента выставляется флаг, и _build_bash_sandbox_settings вернёт None.
    Сервис продолжает работать без bash sandbox (hook sandbox остаётся).

    Это сознательный trade-off: лучше degrade gracefully, чем оставить
    пользователя с мёртвым сервисом после git pull && update.sh, когда
    bwrap ещё не успели поставить.
    """
    import shutil

    # Worker без явного "bubblewrap: false" тоже в списке — дефолт
    # флипнут (см. Agent._build_bash_sandbox_settings). Master не
    # включается никогда без allow_master_bwrap.
    def _wants_bwrap(a: Agent) -> bool:
        sb = a.config.get("sandbox", {})
        default_on = not a.is_master
        if not sb.get("bubblewrap", default_on):
            return False
        if a.is_master and not sb.get("allow_master_bwrap", False):
            return False
        return True

    agents_wanting_bwrap = [a for a in agents if _wants_bwrap(a)]
    if not agents_wanting_bwrap:
        return

    if shutil.which("bwrap"):
        names = ", ".join(a.name for a in agents_wanting_bwrap)
        logger.info(f"Bash sandbox (bwrap) включён для: {names}")
        return

    names = ", ".join(a.name for a in agents_wanting_bwrap)
    logger.warning(
        "⚠ bubblewrap не установлен, но запрошен агентами: %s. "
        "Продолжаем без bash sandbox (hook-only защита остаётся). "
        "Поставь: sudo apt-get install -y bubblewrap — и перезапусти сервис.",
        names,
    )
    # Пометить агентов, чтобы _build_bash_sandbox_settings выключил bwrap
    # на этот запуск. Флаг пересоздаётся на каждом старте по конфигу.
    for a in agents_wanting_bwrap:
        a._bwrap_unavailable = True


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

    # Удалить qmd если остался от предыдущих версий (wiki-поиск теперь встроенный)
    _cleanup_qmd()

    # Загрузить агентов
    agents = load_agents(root)
    if not agents:
        logger.error("Нет агентов для запуска. Проверь agents/*/agent.yaml")
        sys.exit(1)

    # Защита от параллельного запуска (systemd + docker compose, и т.п.)
    _acquire_singleton_lock(agents)

    # Проверить bwrap для агентов с bubblewrap sandbox
    _check_bubblewrap_requirements(agents)

    # Глобальный семафор для Claude CLI
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLAUDE)

    # Инициализировать git для памяти каждого агента
    for agent in agents:
        if memory.git_init(agent.agent_dir):
            logger.info(f"Git memory initialized for '{agent.name}'")

    # Проверить прерванные checkpoint'ы
    from .checkpoint import recover as checkpoint_recover, format_recovery_message, clear as checkpoint_clear
    for agent in agents:
        cp = checkpoint_recover(agent.agent_dir)
        if cp:
            logger.warning(
                f"Agent '{agent.name}': прерванный вызов обнаружен. "
                f"Prompt: {cp.get('prompt', '')[:50]}..."
            )
            # Записать в daily note
            memory.log_message(
                agent.agent_dir, "system",
                format_recovery_message(cp)
            )
            # Сбросить session (прерванная сессия невалидна)
            memory.clear_session_id(agent.agent_dir)
            checkpoint_clear(agent.agent_dir)

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

        # Bridge (Telegram)
        bridge = TelegramBridge(
            agent, semaphore, bus=bus, agent_worker=worker,
            fleet_runtime=runtime,
        )
        bus.subscribe(f"telegram:{agent.name}")
        bot_task = asyncio.create_task(run_bot(bridge))
        tasks.append(bot_task)

        agent_runtime_tasks = [worker_task, bot_task]

        # MAX bridge (опциональный второй транспорт, если задан max_bot_token)
        max_task = _maybe_start_max_bridge(agent, semaphore, bus, worker, runtime)
        if max_task:
            tasks.append(max_task)
            agent_runtime_tasks.append(max_task)

        # Регистрация в runtime (worker + bot tasks)
        runtime.register_running(agent, worker, bridge, agent_runtime_tasks)
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

    # ── SkillAdvisor Receiver + Digest (для master-агентов) ──
    for agent in agents:
        if not agent.is_master:
            continue

        # Receiver — принимает предложения от worker-агентов
        receiver = SkillSuggestionReceiver(agent.agent_dir, agent.name, bus)
        receiver_task = asyncio.create_task(receiver.run())
        if agent.name in runtime.tasks:
            runtime.tasks[agent.name].append(receiver_task)
        tasks.append(receiver_task)

        # Digest loop — ежедневная сводка владельцу
        sa_master_config = agent.config.get("skill_advisor_digest", {})
        digest_hour = sa_master_config.get("hour", 20)
        digest_task = asyncio.create_task(
            skill_digest_loop(
                agent.agent_dir, agent.name, bus,
                check_hour=digest_hour,
            )
        )
        if agent.name in runtime.tasks:
            runtime.tasks[agent.name].append(digest_task)
        tasks.append(digest_task)

        logger.info(
            f"SkillAdvisor запущен для master '{agent.name}': "
            f"receiver + digest (в {digest_hour}:00)"
        )

    # ── Dream Memory ──
    for agent in agents:
        dream_config = agent.config.get("dream", {})
        if dream_config:
            interval = dream_config.get("interval_hours", 2.0)
            model_p1 = dream_config.get("model_phase1", "haiku")
            model_p2 = dream_config.get("model_phase2", "sonnet")

            # Phase 3: SkillAdvisor (для worker-агентов)
            sa_config = agent.config.get("skill_advisor")
            # Phase 3b: SchemaAdvisor (для агентов с vault'ом, напр. archivist)
            schema_adv_config = agent.config.get("schema_advisor")

            dream_task = asyncio.create_task(
                dream_loop(
                    agent.agent_dir, interval, model_p1, model_p2,
                    skill_advisor_config=sa_config,
                    schema_advisor_config=schema_adv_config,
                    bus=bus,
                    agent_name=agent.name,
                )
            )
            # Добавить в runtime
            if agent.name in runtime.tasks:
                runtime.tasks[agent.name].append(dream_task)
            tasks.append(dream_task)
            logger.info(
                f"Dream loop запущен для '{agent.name}' (каждые {interval}ч)"
                + (", skill_advisor включён" if sa_config else "")
                + (", schema_advisor включён" if schema_adv_config else "")
            )

    # ── Heartbeat (smart или legacy) ──
    for agent in agents:
        hb_config = agent.config.get("heartbeat", {})
        if hb_config.get("enabled", False):
            if hb_config.get("triggers"):
                # Smart heartbeat с триггерами
                smart_hb = SmartHeartbeat(
                    agent.agent_dir, agent.name, hb_config, bus=bus,
                    chat_id=_master_notify_chat_id(agent),
                )
                hb_task = asyncio.create_task(smart_hb.run())
                trigger_names = [t["name"] for t in hb_config["triggers"]]
                if agent.name in runtime.tasks:
                    runtime.tasks[agent.name].append(hb_task)
                tasks.append(hb_task)
                logger.info(
                    f"SmartHeartbeat запущен для '{agent.name}': "
                    f"{', '.join(trigger_names)}"
                )
            else:
                # Legacy heartbeat
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
                logger.info(
                    f"Heartbeat запущен для '{agent.name}' (каждые {interval} мин)"
                )

    # ── Cron ──
    # Определяем мастер-агента один раз — worker-агенты маршрутизируют
    # уведомления через его telegram-бота (у них нет прямого chat_id).
    _master_agent = next((a for a in agents if a.is_master), None)
    _master_name = _master_agent.name if _master_agent else None

    for agent in agents:
        if agent.config.get("cron"):
            # Worker-агенты: берём FOUNDER_TELEGRAM_ID и маршрутизируем через
            # мастера, потому что у worker-агентов chat_id=0 (нет прямого чата).
            if agent.is_master:
                cron_chat_id = _master_notify_chat_id(agent)
                cron_notify_agent = None
            else:
                cron_chat_id = _founder_chat_id(root)
                cron_notify_agent = _master_name
            cron_task = asyncio.create_task(
                cron_loop(
                    agent.config,
                    agent.agent_dir,
                    agent.name,
                    bus=bus,
                    chat_id=cron_chat_id,
                    notify_agent_name=cron_notify_agent,
                )
            )
            if agent.name in runtime.tasks:
                runtime.tasks[agent.name].append(cron_task)
            tasks.append(cron_task)
            cron_names = [j["name"] for j in agent.config["cron"]]
            logger.info(f"Cron запущен для '{agent.name}': {', '.join(cron_names)}")

    # ── Reminders ──
    # Восстанавливает CronCreate-напоминания после перезапуска.
    # Запускается для всех агентов безусловно: reminders.json может
    # появиться в любой момент (например от CronCreate внутри turn'а).
    # Worker-агенты: используем founder chat_id (аналогично cron_loop).
    for agent in agents:
        reminders_chat_id = (
            _master_notify_chat_id(agent)
            if agent.is_master
            else _founder_chat_id(root)
        )
        reminders_task = asyncio.create_task(
            reminders_loop(
                agent.agent_dir, agent.name, bus=bus,
                chat_id=reminders_chat_id,
            )
        )
        if agent.name in runtime.tasks:
            runtime.tasks[agent.name].append(reminders_task)
        tasks.append(reminders_task)
        logger.info(f"Reminders loop запущен для '{agent.name}'")

    # ── Dispatcher ──
    # Смотрит memory/dispatch/ у каждого агента через inotify (fallback —
    # поллинг 5 сек) и публикует готовые сообщения в bus с явным
    # chat_id+thread_id. Запускается для всех агентов.
    for agent in agents:
        dispatch_task = asyncio.create_task(
            dispatcher_loop(agent.agent_dir, agent.name, bus=bus)
        )
        if agent.name in runtime.tasks:
            runtime.tasks[agent.name].append(dispatch_task)
        tasks.append(dispatch_task)
        logger.info(f"Dispatcher запущен для '{agent.name}'")

    # ── Knowledge Graph (ночной пайплайн связей) ──
    for agent in agents:
        kg_config = agent.config.get("knowledge_graph", {})
        if kg_config.get("enabled", False):
            run_hour = kg_config.get("run_hour", 1)
            run_minute = kg_config.get("run_minute", 0)
            kg_task = asyncio.create_task(
                nightly_graph_loop(
                    agent.agent_dir,
                    config=kg_config,
                    run_hour=run_hour,
                    run_minute=run_minute,
                )
            )
            if agent.name in runtime.tasks:
                runtime.tasks[agent.name].append(kg_task)
            tasks.append(kg_task)
            synthesis_schedule = kg_config.get("synthesis_schedule", {})
            daily_phase = synthesis_schedule.get("daily_phase_days", 14)
            regular = synthesis_schedule.get("regular_interval_days", 3)
            logger.info(
                f"Knowledge Graph запущен для '{agent.name}': "
                f"{run_hour:02d}:{run_minute:02d} UTC, "
                f"L3: ежедневно {daily_phase}д → каждые {regular}д"
            )

    # ── GitHub Sync (ночное резервное копирование) ──
    github_sync_task = asyncio.create_task(
        github_sync_loop(root, run_hour=3, run_minute=0)
    )
    tasks.append(github_sync_task)
    logger.info("GitHub Sync loop запущен: ежедневно в 03:00 UTC")

    # ── AutoCompact (фоновое сжатие контекста в простое) ──
    # Дополняет post-turn consolidation: ловит idle-окна между turn'ами
    # и жмёт накопленный контекст, чтобы следующий turn стартовал
    # лёгким. Inspired by nanobot HKUDS двухуровневый AutoCompact.
    from .auto_compact import auto_compact_loop
    auto_compact_task = asyncio.create_task(auto_compact_loop(runtime))
    tasks.append(auto_compact_task)
    logger.info("AutoCompact loop запущен (idle compaction каждые 5 мин)")

    # ── HTTP sidecar (Mini App + A2A) ──
    # Не запускается если HTTP_PORT пуст/0. Логирует свой статус сам.
    http_task = asyncio.create_task(http_serve_forever(runtime))
    tasks.append(http_task)

    logger.info(
        f"Fleet запущен: {len(agents)} агентов, "
        f"{len(tasks)} задач. Ctrl+C для остановки."
    )

    try:
        # return_exceptions=True — изоляция флота: падение ОДНОЙ таски (бот,
        # MAX-мост, фоновый цикл) больше НЕ роняет весь процесс. Раньше любой
        # неперехваченный эксепшен в одной таске пробрасывался сюда и убивал
        # всех (см. инцидент 2026-06-20: битый токен МАКС → весь парк в
        # restart-loop). Упавшие таски логируем, чтобы сбой не утонул молча.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(
                r, asyncio.CancelledError
            ):
                logger.error(
                    f"Таска флота завершилась с ошибкой: {type(r).__name__}: {r}"
                )
    except asyncio.CancelledError:
        orchestrator.stop()
    finally:
        # Flush любые отложенные git-коммиты до выхода
        from . import git_committer
        try:
            await git_committer.flush()
        except Exception as e:
            logger.warning(f"git_committer.flush на shutdown: {e}")


def main() -> None:
    """Точка входа (синхронная)."""
    try:
        import uvloop
    except ImportError:
        logger.info("uvloop unavailable, using default asyncio event loop")
    else:
        uvloop.install()
        logger.info("uvloop enabled")
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")


if __name__ == "__main__":
    main()
