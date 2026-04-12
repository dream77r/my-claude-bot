"""
Класс Agent — загрузка YAML конфига, вызов Claude через claude-agent-sdk.

Каждый агент:
- Загружает agent.yaml с expandvars для секретов
- Управляет сессией Claude (--resume)
- Собирает system prompt: SOUL.md + skills + memory context
- Обрабатывает сообщения через asyncio.Queue
"""

import asyncio
import logging
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from . import memory
from . import get_claude_cli_path
from .audit import make_audit_hook
from .checkpoint import make_checkpoint_hooks
from .command_guard import make_guard_hook
from .consolidator import Consolidator
from .hooks import HookContext, HookRegistry
from .i18n import t
from .input_sanitizer import make_sanitizer_hook
from .metrics import make_metrics_hook
from .sandbox import make_sandbox_hook
from .ssrf_protection import make_ssrf_hook
from .tool_hints import format_tool_hint

logger = logging.getLogger(__name__)


class Agent:
    def __init__(self, config_path: str):
        """
        Инициализация агента из YAML конфига.

        Args:
            config_path: путь к agent.yaml
        """
        self.config_path = Path(config_path)
        self.agent_dir = str(self.config_path.parent)
        self.config = self._load_config()

        self.name: str = self.config["name"]
        self.display_name: str = self.config.get("display_name", self.name)
        self.role: str = self.config.get("role", "worker")
        self.bot_token: str = self.config["bot_token"]
        self.system_prompt_template: str = self.config.get("system_prompt", "")
        self.memory_path: str = self.config.get("memory_path", f"./agents/{self.name}/memory/")
        self.skill_names: list[str] = self.config.get("skills", [])
        self.allowed_users: list[int] = self._parse_allowed_users()
        self.max_context_messages: int = self.config.get("max_context_messages", 50)
        self.claude_model: str = self.config.get("claude_model", "sonnet")
        self.claude_flags: list[str] = self.config.get("claude_flags", [])
        self.mcp_servers: dict = self.config.get("mcp_servers", {})

        # Очередь сообщений (сериализация обработки)
        self.queue: asyncio.Queue = asyncio.Queue()

        # Семафор для ограничения параллельных вызовов Claude
        self._semaphore: asyncio.Semaphore | None = None

        # Hook-система (lifecycle hooks)
        self.hooks = HookRegistry()

        # Input Sanitizer — before_call хук (защита от prompt injection)
        sanitizer_enabled = self.config.get("input_sanitizer", {}).get("enabled", True)
        if sanitizer_enabled:
            self.hooks.register_fn(
                "before_call", "input_sanitizer", make_sanitizer_hook()
            )

        # Command Guard — on_tool_use хук (логирование опасных команд)
        guard_enabled = self.config.get("command_guard", {}).get("enabled", True)
        if guard_enabled:
            self.hooks.register_fn(
                "on_tool_use", "command_guard", make_guard_hook()
            )

        # SSRF Protection — on_tool_use хук (блокировка fetch к внутренним IP)
        ssrf_config = self.config.get("ssrf_protection", {})
        ssrf_enabled = ssrf_config.get("enabled", True)
        if ssrf_enabled:
            ssrf_whitelist = ssrf_config.get("whitelist", [])
            self.hooks.register_fn(
                "on_tool_use", "ssrf_protection",
                make_ssrf_hook(ssrf_whitelist or None)
            )

        # Sandbox — изоляция файловой системы для worker-агентов
        sandbox_config = self.config.get("sandbox", {})
        # По умолчанию: worker = sandbox ON, master = sandbox OFF
        sandbox_default = not self.is_master
        sandbox_enabled = sandbox_config.get("enabled", sandbox_default)
        if sandbox_enabled:
            sandbox_root = str(Path(self.agent_dir).resolve())
            sandbox_extra = sandbox_config.get("allowed_paths", [])
            self.hooks.register_fn(
                "on_tool_use", "sandbox",
                make_sandbox_hook(sandbox_root, sandbox_extra or None)
            )

        # Audit Logging — JSONL аудит-лог всех tool calls
        audit_enabled = self.config.get("audit", {}).get("enabled", True)
        if audit_enabled:
            self.hooks.register_fn(
                "on_tool_use", "audit",
                make_audit_hook(self.config_path.parent.as_posix())
            )

        # Checkpoint Recovery — сохранение состояния при крэше
        checkpoint_enabled = self.config.get("checkpoint", {}).get("enabled", True)
        if checkpoint_enabled:
            cp_before, cp_tool, cp_after, cp_error = make_checkpoint_hooks(
                self.config_path.parent.as_posix()
            )
            self.hooks.register_fn("before_call", "checkpoint_before", cp_before)
            self.hooks.register_fn("on_tool_use", "checkpoint_tool", cp_tool)
            self.hooks.register_fn("after_call", "checkpoint_after", cp_after)
            self.hooks.register_fn("on_error", "checkpoint_error", cp_error)

        # Metrics — автоматическое логирование использования
        metrics_enabled = self.config.get("metrics", {}).get("enabled", True)
        if metrics_enabled:
            before_fn, tool_fn, after_fn, error_fn = make_metrics_hook(
                self.config_path.parent.as_posix(), self.claude_model
            )
            self.hooks.register_fn("before_call", "metrics_before", before_fn)
            self.hooks.register_fn("on_tool_use", "metrics_tool", tool_fn)
            self.hooks.register_fn("after_call", "metrics_after", after_fn)
            self.hooks.register_fn("on_error", "metrics_error", error_fn)

        # Consolidator — сжатие контекста при длинных разговорах
        consolidator_config = self.config.get("consolidator", {})
        if consolidator_config.get("enabled", True):
            self.consolidator: Consolidator | None = Consolidator(
                self.config_path.parent.as_posix(), consolidator_config
            )
        else:
            self.consolidator = None

        # Инициализация памяти
        memory.ensure_dirs(self.agent_dir)

        logger.info(f"Agent '{self.name}' загружен из {config_path}")

    def _load_config(self) -> dict:
        """Загрузить и обработать YAML конфиг с expandvars."""
        with open(self.config_path, encoding="utf-8") as f:
            raw = f.read()

        # expandvars для секретов (${ME_BOT_TOKEN} → значение из .env)
        expanded = os.path.expandvars(raw)
        return yaml.safe_load(expanded)

    def _parse_allowed_users(self) -> list[int]:
        """Разобрать список allowed_users, преобразовать в int."""
        raw = self.config.get("allowed_users", [])
        result = []
        for u in raw:
            try:
                result.append(int(u))
            except (ValueError, TypeError):
                logger.warning(f"Невалидный user ID: {u}")
        return result

    @property
    def is_master(self) -> bool:
        """Является ли агент мастером (оркестратором) флота."""
        return self.role == "master"

    def is_user_allowed(self, user_id: int) -> bool:
        """Проверить, имеет ли пользователь доступ."""
        if not self.allowed_users:
            return True  # Если список пуст — доступ всем
        return user_id in self.allowed_users

    @staticmethod
    def parse_skill_frontmatter(text: str) -> tuple[dict | None, str]:
        """
        Разобрать YAML frontmatter из skill файла.

        Returns:
            (metadata dict или None, тело скилла без frontmatter)
        """
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not match:
            return None, text
        try:
            meta = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None, text
        body = text[match.end():]
        return meta, body

    @staticmethod
    def check_skill_requirements(meta: dict) -> tuple[bool, list[str]]:
        """
        Проверить жёсткие зависимости скилла (команды, env-переменные).

        Returns:
            (ok, список ошибок) — если ok=False, скилл должен быть отключён
        """
        errors = []
        reqs = meta.get("requirements", {})
        for cmd in reqs.get("commands", []):
            if not shutil.which(cmd):
                errors.append(f"команда '{cmd}' не найдена")
        for env_var in reqs.get("env", []):
            if not os.environ.get(env_var):
                errors.append(f"переменная '{env_var}' не задана")
        return len(errors) == 0, errors

    @staticmethod
    def match_skill_triggers(user_message: str, meta: dict) -> bool:
        """
        Проверить совпадает ли сообщение пользователя с триггерами скилла.

        Поддерживаемые поля в meta["triggers"]:
            keywords: список подстрок (case-insensitive, substring-match)
            file_extensions: список расширений (напр. ".pdf")

        Args:
            user_message: текст сообщения пользователя
            meta: распарсенный frontmatter скилла

        Returns:
            True если хотя бы один триггер совпал
        """
        triggers = meta.get("triggers") or {}
        if not triggers:
            return False
        msg_lower = user_message.lower()
        for keyword in triggers.get("keywords") or []:
            if str(keyword).lower() in msg_lower:
                return True
        for ext in triggers.get("file_extensions") or []:
            if str(ext).lower() in msg_lower:
                return True
        return False

    @staticmethod
    def check_skill_memory_requirements(meta: dict, memory_path: str) -> list[str]:
        """
        Проверить декларированные файлы памяти (requires_memory).

        В отличие от check_skill_requirements это мягкая проверка: отсутствие
        файла памяти НЕ отключает скилл. Возвращается список отсутствующих
        файлов, чтобы вызывающий код мог показать подсказку пользователю или
        залогировать warning.

        Args:
            meta: распарсенный frontmatter скилла
            memory_path: путь к папке памяти агента

        Returns:
            Список относительных путей к отсутствующим файлам (пустой — всё ок)
        """
        required = meta.get("requires_memory") or []
        if not required:
            return []
        missing = []
        base = Path(memory_path)
        for rel in required:
            if not (base / rel).exists():
                missing.append(rel)
        return missing

    def _load_skills(self, user_message: str | None = None) -> str:
        """
        Загрузить скиллы из agents/{name}/skills/*.md.

        Поддерживает YAML frontmatter (agentskills.io-совместимый):
        - name, version, description, license, when_to_use, tags
        - triggers: {keywords, file_extensions} — для progressive disclosure
        - requires_memory: список файлов памяти (мягкая проверка)
        - requirements.commands/env: жёсткие проверки
        - always: true = всегда полное тело в system prompt

        Режимы работы:
        - user_message=None (legacy): полное тело каждого активного скилла
          попадает в system prompt. Поведение совместимо со старым кодом.
        - user_message=str (progressive): в system prompt попадают только
          метаданные (каталог). Полное тело подгружается только для:
            a) скиллов с always=true
            b) скиллов, чьи триггеры совпали с сообщением пользователя
          Активация живёт только внутри одного вызова — при следующем
          сообщении pattern-match запускается заново.

        Returns:
            Строка для включения в system prompt (может быть пустой)
        """
        skills_dir = Path(self.agent_dir) / "skills"
        if not skills_dir.exists():
            return ""

        eligible: list[tuple[str, dict | None, str]] = []  # (name, meta, body)

        for skill_file in sorted(skills_dir.glob("*.md")):
            raw = skill_file.read_text(encoding="utf-8")
            meta, body = self.parse_skill_frontmatter(raw)
            skill_name = skill_file.stem

            # Фильтрация: если не always и не в списке skills из yaml — пропустить
            if meta and not meta.get("always", False):
                if self.skill_names and skill_name not in self.skill_names:
                    continue

            # Проверка жёстких зависимостей (команды, env)
            if meta:
                ok, errors = self.check_skill_requirements(meta)
                if not ok:
                    logger.warning(
                        f"Скилл '{skill_name}' отключён: {', '.join(errors)}"
                    )
                    continue

                # Мягкая проверка файлов памяти (requires_memory)
                missing_memory = self.check_skill_memory_requirements(
                    meta, self.memory_path
                )
                if missing_memory:
                    logger.warning(
                        f"Скилл '{skill_name}' активен, но ждёт файлы памяти: "
                        f"{', '.join(missing_memory)}"
                    )

            eligible.append((skill_name, meta, body if meta else raw))

        if not eligible:
            return ""

        # Legacy-режим: полное тело всех подходящих скиллов
        if user_message is None:
            return "\n\n---\n\n".join(
                (body if body else "").strip() for _, _, body in eligible
            )

        # Progressive-режим: метаданные всех + полные тела только активированных
        catalog_lines: list[str] = []
        full_bodies: list[str] = []
        activated: list[str] = []

        for skill_name, meta, body in eligible:
            if meta is None:
                # Legacy-скилл без frontmatter — грузим как есть (нет метаданных)
                full_bodies.append(body.strip())
                continue

            always = bool(meta.get("always", False))
            triggered = self.match_skill_triggers(user_message, meta)

            if always or triggered:
                full_bodies.append(body.strip())
                if triggered and not always:
                    activated.append(skill_name)
            else:
                # Только метаданные в каталог
                desc = meta.get("description", "").strip() or meta.get("when_to_use", "")
                when = meta.get("when_to_use", "")
                line = f"- **{skill_name}** — {desc}"
                if when and when != desc:
                    line += f" _(активируется когда: {when})_"
                catalog_lines.append(line)

        if activated:
            logger.info(
                f"[{self.name}] Progressive disclosure: активированы скиллы "
                f"{', '.join(activated)} для сообщения длиной {len(user_message)} симв."
            )

        sections: list[str] = []
        if catalog_lines:
            sections.append(
                "### Каталог доступных скиллов (метаданные)\n\n"
                + "\n".join(catalog_lines)
                + "\n\n"
                + "_Если задача требует одного из этих скиллов, а его полная "
                "инструкция не видна, попроси пользователя явно назвать скилл._"
            )
        if full_bodies:
            sections.append("\n\n---\n\n".join(full_bodies))

        return "\n\n---\n\n".join(sections)

    def _load_soul(self) -> str:
        """Загрузить SOUL.md из директории агента."""
        soul_path = Path(self.agent_dir) / "SOUL.md"
        if soul_path.exists():
            return soul_path.read_text(encoding="utf-8")
        return ""

    def _build_fleet_context(self) -> str:
        """
        Описать подчинённых агентов для делегации.

        Только для master-агента. Включает:
        - Список доступных worker-ов с описанием
        - Инструкцию по делегации через файлы
        - Пути к данным worker-ов (чтение/настройки)
        """
        if not self.is_master:
            return ""

        agents_dir = Path(self.agent_dir).parent
        fleet_info = []
        worker_paths = []

        for agent_yaml in sorted(agents_dir.glob("*/agent.yaml")):
            if agent_yaml.parent.name == self.name:
                continue
            try:
                with open(agent_yaml, encoding="utf-8") as f:
                    raw = f.read()
                # БЕЗ expandvars — не раскрывать секреты
                config = yaml.safe_load(raw)
                name = config.get("name", "")
                display = config.get("display_name", name)
                desc = config.get("system_prompt", "")[:200].strip()
                fleet_info.append(f"- **{name}** ({display}): {desc}")

                # Пути к данным worker-а
                worker_dir = str(agent_yaml.parent.resolve())
                worker_paths.append(
                    f"- **{name}**: память `{worker_dir}/memory/`, "
                    f"конфиг `{worker_dir}/agent.yaml`"
                )
            except Exception:
                continue

        if not fleet_info:
            return ""

        delegation_dir = Path(self.agent_dir) / "memory" / "delegation"

        parts = [
            "## Управление командой агентов\n",
            "Ты — главный агент (master). У тебя есть подчинённые агенты, "
            "которым ты можешь давать задания.\n",
            "### Подчинённые агенты:\n" + "\n".join(fleet_info) + "\n",
            "### Делегация задач:\n"
            f"1. Запиши задачу: `Write` → `{delegation_dir}/{{agent_name}}.task.md`\n"
            "2. Подожди ~10-15 секунд\n"
            f"3. Прочитай ответ: `Read` → `{delegation_dir}/{{agent_name}}.result.md`\n",
            "Формат файла задачи:\n"
            "```\n"
            "Описание задачи для агента...\n"
            "```\n",
            "### Доступ к данным агентов:\n"
            "Ты можешь читать и изменять данные любого подчинённого агента:\n"
            + "\n".join(worker_paths) + "\n",
            "Ты можешь:\n"
            "- Читать wiki, daily notes, profile любого агента\n"
            "- Изменять настройки агентов (agent.yaml)\n"
            "- Читать и редактировать их скиллы\n"
            "- Смотреть логи их работы\n",
            "Используй делегацию когда задача лучше подходит другому агенту "
            "(код — кодеру, командные задачи — team). "
            "Если нужен только доступ к данным — читай напрямую.\n",
        ]

        return "\n".join(parts)

    def _build_worker_isolation(self) -> str:
        """
        Инструкции изоляции для worker-агентов.

        Worker не должен:
        - Делегировать задачи другим агентам
        - Читать/изменять данные master-агента
        - Изменять свой agent.yaml
        """
        if self.is_master:
            return ""

        agents_dir = Path(self.agent_dir).parent
        master_name = None
        for agent_yaml in agents_dir.glob("*/agent.yaml"):
            try:
                with open(agent_yaml, encoding="utf-8") as f:
                    config = yaml.safe_load(f.read())
                if config.get("role") == "master":
                    master_name = config.get("name", agent_yaml.parent.name)
                    break
            except Exception:
                continue

        parts = [
            "## Режим работы\n",
            "Ты — подчинённый агент (worker). Ты получаешь задачи от "
            f"главного агента{f' ({master_name})' if master_name else ''} "
            "или напрямую от пользователя.\n",
            "### Ограничения:\n"
            "- Ты НЕ можешь давать задания другим агентам\n"
            "- Ты НЕ можешь читать или изменять данные других агентов\n"
            "- Ты НЕ можешь изменять свой agent.yaml\n"
            "- Работай только в своей директории памяти\n",
            "Если тебе нужна информация от другого агента или действие "
            "за пределами твоих полномочий — сообщи об этом в ответе, "
            "и главный агент решит что делать.\n",
        ]

        return "\n".join(parts)

    @staticmethod
    def _build_file_instructions(outbox_path: str) -> str:
        """Инструкция для Claude: как отправить файл пользователю через outbox."""
        return (
            "## Работа с файлами\n\n"
            "Пользователь может отправлять тебе файлы для анализа. "
            "Пути к файлам указаны в сообщении — читай их через Read.\n\n"
            "### Отправка файлов пользователю\n"
            "Чтобы отправить файл обратно пользователю в Telegram, "
            f"сохрани его в директорию outbox: `{outbox_path}/`\n\n"
            "Примеры:\n"
            f"- Изменённый файл: `Write` → `{outbox_path}/result.txt`\n"
            f"- Отчёт: `Write` → `{outbox_path}/report.md`\n"
            f"- Исправленный код: `Write` → `{outbox_path}/fixed_code.py`\n\n"
            "Правила:\n"
            "- Имя файла в outbox = имя файла, которое получит пользователь\n"
            "- Можно отправить несколько файлов за раз\n"
            "- Файлы из outbox автоматически отправятся после твоего ответа\n"
            "- Если пользователь просит проанализировать файл — "
            "просто ответь текстом, без outbox\n"
            "- Используй outbox только когда пользователь явно просит "
            "отправить/вернуть/сохранить файл"
        )

    def build_system_prompt(self, user_query: str = "") -> str:
        """
        Собрать полный system prompt:
        SOUL.md + system_prompt из YAML + skills + memory context

        Args:
            user_query: текущий запрос пользователя (для wiki search)
        """
        parts = []

        # 1. SOUL.md — личность агента
        soul = self._load_soul()
        if soul:
            parts.append(soul)

        # 2. System prompt из agent.yaml
        if self.system_prompt_template:
            parts.append(self.system_prompt_template)

        # 3. Скиллы (progressive disclosure если есть запрос пользователя)
        skills = self._load_skills(user_message=user_query or None)
        if skills:
            parts.append("## Скиллы\n\n" + skills)

        # 4. Контекст из памяти (smart: profile, hot pages, wiki search, daily, index)
        ctx = memory.build_smart_context(self.agent_dir, user_query=user_query)
        if ctx:
            parts.append("## Контекст из памяти\n\n" + ctx)

        # 5. Контекст флота (master) или изоляция (worker)
        fleet = self._build_fleet_context()
        if fleet:
            parts.append(fleet)
        isolation = self._build_worker_isolation()
        if isolation:
            parts.append(isolation)

        # 6. Сводка от Consolidator (если был сброс сессии)
        if self.consolidator:
            summary = self.consolidator.get_summary()
            if summary:
                parts.append(
                    "## Сводка предыдущего разговора\n\n"
                    "Контекст был сжат. Вот краткая сводка:\n\n" + summary
                )

        # 7. Инструкция по работе с файлами (outbox)
        outbox_path = Path(self.agent_dir) / "memory" / "outbox"
        parts.append(self._build_file_instructions(str(outbox_path.resolve())))

        # 8. Языковая инструкция
        lang = memory.get_setting(self.agent_dir, "language")
        if lang:
            parts.append(t("system_lang_instruction", lang))

        return "\n\n---\n\n".join(parts)

    def build_group_system_prompt(self, chat_id: int) -> str:
        """
        Собрать system prompt для группового чата.

        Отличия от build_system_prompt:
        - Вместо profile.md → groups/{chat_id}/context.md
        - Вместо личного daily → groups/{chat_id}/daily/
        - Добавляет инструкцию для группового режима
        - НЕ включает личную wiki/profile владельца
        """
        parts = []

        # 1. SOUL.md — личность (общая)
        soul = self._load_soul()
        if soul:
            parts.append(soul)

        # 2. System prompt из agent.yaml (общий)
        if self.system_prompt_template:
            parts.append(self.system_prompt_template)

        # 3. Инструкция для группового режима
        group_instructions = (
            "## Режим группового чата\n\n"
            "Ты в групповом чате. Правила:\n"
            "- Отвечай только когда к тебе обращаются\n"
            "- Учитывай контекст предыдущих сообщений (они в логе ниже)\n"
            "- Обращайся к участникам по имени\n"
            "- Не выдавай личную информацию пользователя-владельца\n"
            "- Будь краток — в групповом чате никто не читает длинные ответы"
        )
        parts.append(group_instructions)

        # 4. Скиллы (общие)
        skills = self._load_skills()
        if skills:
            parts.append("## Скиллы\n\n" + skills)

        # 5. Контекст группы (вместо личного)
        ctx = memory.read_group_context(self.agent_dir, chat_id)
        if ctx:
            parts.append("## Контекст из памяти\n\n" + ctx)

        # 6. Инструкция по работе с файлами (outbox)
        outbox_path = Path(self.agent_dir) / "memory" / "outbox"
        parts.append(self._build_file_instructions(str(outbox_path.resolve())))

        return "\n\n---\n\n".join(parts)

    def _parse_allowed_tools(self) -> list[str] | None:
        """Извлечь allowed tools из claude_flags."""
        flags = self.claude_flags
        for i, flag in enumerate(flags):
            if flag == "--allowedTools" and i + 1 < len(flags):
                return flags[i + 1].split(",")
        return None

    async def call_claude(
        self,
        message: str,
        files: list[str] | None = None,
        semaphore: asyncio.Semaphore | None = None,
        on_tool_use: Callable[[str], Awaitable[None]] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        group_chat_id: int | None = None,
    ) -> str:
        """
        Вызвать Claude через claude-agent-sdk.

        Args:
            message: текст сообщения от пользователя
            files: список путей к файлам (будут упомянуты в промпте)
            semaphore: глобальный семафор для ограничения параллельных вызовов
            on_tool_use: async callback, вызывается с tool hint строкой
                         при каждом использовании инструмента
            on_text_delta: async callback, вызывается при каждом TextBlock
                           для streaming ответа
            group_chat_id: ID группового чата (None для DM)

        Returns:
            Текстовый ответ от Claude
        """
        sem = semaphore or self._semaphore

        # Подготовить промпт с файлами
        prompt = message
        if files:
            file_list = "\n".join(f"- {f}" for f in files)
            prompt = f"{message}\n\nПрикреплённые файлы:\n{file_list}"

        # System prompt — изолированный для групп
        if group_chat_id is not None:
            system_prompt = self.build_group_system_prompt(group_chat_id)
        else:
            system_prompt = self.build_system_prompt(user_query=message)

        # Allowed tools
        allowed_tools = self._parse_allowed_tools()

        # Memory path для cwd
        memory_path = Path(self.agent_dir) / "memory"

        # Session ID для --resume
        session_id = memory.get_session_id(self.agent_dir)

        # Перехват stderr для диагностики
        stderr_lines = []
        def _on_stderr(line: str):
            stderr_lines.append(line)
            logger.warning(f"Claude stderr: {line.strip()}")

        # Модель: settings override → agent.yaml default
        active_model = memory.get_setting(self.agent_dir, "claude_model") or self.claude_model

        # CLI hooks для Claude Code
        cli_hooks = {
            "PreCompact": [
                {
                    "type": "command",
                    "command": (
                        f'echo "\\n## Компакт сессии $(date +%H:%M)\\n'
                        f'Контекст сжат. Ключевая информация сохранена в profile.md и wiki/." '
                        f'>> {memory_path.resolve()}/daily/$(date +%Y-%m-%d).md'
                    ),
                }
            ],
        }

        # Command Guard — PreToolUse хук для реальной блокировки
        guard_config = self.config.get("command_guard", {})
        pre_tool_hooks = []
        if guard_config.get("enabled", True):
            guard_script = Path(__file__).parent / "command_guard.py"
            pre_tool_hooks.append({
                "type": "command",
                "command": f"{sys.executable} {guard_script}",
            })

        # Sandbox — PreToolUse хук для изоляции worker-агентов
        sandbox_config = self.config.get("sandbox", {})
        sandbox_default = not self.is_master
        if sandbox_config.get("enabled", sandbox_default):
            sandbox_script = Path(__file__).parent / "sandbox.py"
            sandbox_root = str(Path(self.agent_dir).resolve())
            sandbox_extra = sandbox_config.get("allowed_paths", [])
            sandbox_args = f'"{sandbox_root}"'
            for p in sandbox_extra:
                sandbox_args += f' "{p}"'
            pre_tool_hooks.append({
                "type": "command",
                "command": f"{sys.executable} {sandbox_script} {sandbox_args}",
            })

        if pre_tool_hooks:
            cli_hooks["PreToolUse"] = pre_tool_hooks

        # Собрать опции
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=str(memory_path.resolve()),
            permission_mode="bypassPermissions",
            model=active_model,
            cli_path=get_claude_cli_path(),
            stderr=_on_stderr,
            hooks=cli_hooks,
        )

        if allowed_tools:
            options.allowed_tools = allowed_tools

        if self.mcp_servers:
            options.mcp_servers = self.mcp_servers

        if session_id:
            options.resume = session_id

        async def _process_message(msg, result_text: str, new_session_id: str | None):
            """Обработать одно сообщение из потока Claude."""
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text
                        if on_text_delta:
                            try:
                                await on_text_delta(result_text)
                            except Exception:
                                pass
                    elif isinstance(block, ToolUseBlock):
                        # Hook: on_tool_use
                        await self.hooks.emit("on_tool_use", HookContext(
                            event="on_tool_use",
                            agent_name=self.name,
                            data={
                                "tool_name": block.name,
                                "tool_input": block.input,
                            },
                        ))
                        if on_tool_use:
                            hint = format_tool_hint(block.name, block.input)
                            try:
                                await on_tool_use(hint)
                            except Exception:
                                pass  # Не ломаем agent loop из-за UI
                if msg.session_id:
                    new_session_id = msg.session_id
            elif isinstance(msg, ResultMessage):
                if msg.session_id:
                    new_session_id = msg.session_id
                if msg.result and not result_text:
                    result_text = msg.result
            return result_text, new_session_id

        # Вызов с семафором
        async def _do_query() -> str:
            result_text = ""
            new_session_id = None

            # Hook: before_call
            before_ctx = await self.hooks.emit("before_call", HookContext(
                event="before_call",
                agent_name=self.name,
                data={"message": prompt, "system_prompt": system_prompt},
            ))
            # Хук может модифицировать промпт
            prompt_final = before_ctx.data.get("message", prompt)
            if prompt_final != prompt:
                options.system_prompt = before_ctx.data.get(
                    "system_prompt", system_prompt
                )

            try:
                async for msg in query(prompt=prompt_final, options=options):
                    result_text, new_session_id = await _process_message(
                        msg, result_text, new_session_id
                    )

            except Exception as e:
                error_name = type(e).__name__
                logger.error(f"Claude SDK error ({error_name}): {e}")

                # Hook: on_error
                await self.hooks.emit("on_error", HookContext(
                    event="on_error",
                    agent_name=self.name,
                    data={"error": e, "message": prompt_final},
                ))

                # Retry без --resume если сессия потеряна
                stderr_text = " ".join(stderr_lines).lower()
                has_session_error = "session" in stderr_text or "conversation" in stderr_text or "not found" in stderr_text
                if session_id and has_session_error:
                    logger.info("Сессия потеряна, создаю новую")
                    memory.clear_session_id(self.agent_dir)
                    options.resume = None
                    try:
                        async for msg in query(prompt=prompt_final, options=options):
                            result_text, new_session_id = await _process_message(
                                msg, result_text, new_session_id
                            )
                    except Exception as retry_err:
                        logger.error(f"Retry failed: {retry_err}")
                        return "Произошла ошибка. Попробуй ещё раз."
                else:
                    return "Произошла ошибка. Попробуй ещё раз."

            # Сохранить session_id
            if new_session_id:
                memory.save_session_id(self.agent_dir, new_session_id)

            # Auto-commit памяти после каждого ответа
            memory.git_commit(self.agent_dir)

            # Hook: after_call
            await self.hooks.emit("after_call", HookContext(
                event="after_call",
                agent_name=self.name,
                data={
                    "message": prompt_final,
                    "response": result_text,
                },
            ))

            # Consolidator: трекинг + автосжатие
            if self.consolidator and result_text:
                self.consolidator.track(prompt_final, result_text)
                if self.consolidator.needs_consolidation():
                    await self.consolidator.consolidate()

            return result_text or "Не удалось получить ответ."

        if sem:
            async with sem:
                return await asyncio.wait_for(_do_query(), timeout=600)
        else:
            return await asyncio.wait_for(_do_query(), timeout=600)
