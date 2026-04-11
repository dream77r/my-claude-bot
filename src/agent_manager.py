"""
Agent Manager — CRUD-операции для агентов.

Создание, валидация, список агентов. Используется из CLI и Telegram-команд.
"""

import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Шаблон agent.yaml для нового агента
AGENT_YAML_TEMPLATE = """\
name: "{name}"
display_name: "{display_name}"
bot_token: "${{{env_var}}}"
system_prompt: |
  Ты — {description}.

  ## Память
  У тебя есть wiki-система для хранения знаний.
  Когда узнаёшь новую важную информацию:
  1. Обнови wiki-страницу через Write
  2. Обнови index.md
  3. Запиши в log.md

  ## Стиль
  - Общайся на русском
  - Будь кратким и конкретным
  - Предлагай действия, а не просто информацию

memory_path: "./agents/{name}/memory/"
skills: []
allowed_users:
{allowed_users_yaml}max_context_messages: 50
claude_model: "{model}"
claude_flags:
  - "--allowedTools"
  - "Read,Write,Glob,Grep,WebSearch,WebFetch"
  - "--output-format"
  - "text"

dream:
  interval_hours: 3
  model_phase1: "haiku"
  model_phase2: "sonnet"

heartbeat:
  enabled: false
"""

# Шаблон SOUL.md
SOUL_MD_TEMPLATE = """\
# SOUL: {display_name}

## Идентичность
Ты — {description}. Работаешь через Telegram и имеешь доступ к файловой системе
для чтения/записи знаний.

## Принципы
1. **Краткость** — отвечай по делу, не лей воду
2. **Действия** — предлагай конкретные шаги, не абстракции
3. **Память** — запоминай важное, ссылайся на прошлые обсуждения
4. **Честность** — если не знаешь — скажи, если сомневаешься — предупреди
5. **Контекст** — учитывай роль, задачи и приоритеты пользователя

## Онбординг (первый запуск)
При первом общении с пользователем:
1. Представься: кто ты и что умеешь (включая голосовые сообщения)
2. Попроси рассказать о себе: имя, роль, компания, проекты, приоритеты
3. Спроси про стиль общения
4. Когда пользователь ответит — запиши всё в profile.md через Write
5. Подтверди что запомнил

## Рабочий процесс
1. Прочитай profile.md для контекста о пользователе
2. Проверь index.md для релевантных знаний
3. Обработай запрос
4. Если получена новая важная информация → обнови wiki
5. Ответь кратко и по делу

## Управление знаниями
Когда узнаёшь новую важную информацию:
- **Люди** → запиши в wiki/entities/
- **Идеи/решения** → запиши в wiki/concepts/
- Обнови index.md с ссылкой на новую страницу

## Стиль общения
- Русский язык
- Без формальностей, но уважительно
- Структурированные ответы для сложных тем
- Короткие ответы для простых вопросов
"""

# Regex для валидации токена BotFather
BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")

# Допустимые имена агентов
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class AgentManager:
    """CRUD-менеджер агентов."""

    def __init__(self, root: Path):
        self.root = root
        self.agents_dir = root / "agents"
        self.env_file = root / ".env"

    def create_agent(
        self,
        name: str,
        display_name: str,
        bot_token: str,
        description: str,
        model: str = "sonnet",
        soul_md: str | None = None,
        allowed_users: list[int] | None = None,
    ) -> Path:
        """
        Создать всю структуру агента + записать токен в .env.

        Args:
            name: имя агента (латиница, для папки)
            display_name: отображаемое имя (русский)
            bot_token: токен от @BotFather
            description: описание роли (одно предложение)
            model: модель Claude (haiku/sonnet/opus)
            soul_md: кастомный SOUL.md (если None — используется шаблон)
            allowed_users: список Telegram user ID (если None — только FOUNDER)

        Returns:
            Path к созданной директории агента

        Raises:
            ValueError: если параметры невалидны
            FileExistsError: если агент с таким именем уже существует
        """
        # Валидация
        errors = self._validate_create_params(name, bot_token, model)
        if errors:
            raise ValueError("; ".join(errors))

        agent_dir = self.agents_dir / name
        if agent_dir.exists():
            raise FileExistsError(f"Агент '{name}' уже существует")

        # Создать структуру директорий
        agent_dir.mkdir(parents=True)
        (agent_dir / "memory").mkdir()
        (agent_dir / "memory" / "wiki").mkdir()
        (agent_dir / "memory" / "wiki" / "entities").mkdir()
        (agent_dir / "memory" / "wiki" / "concepts").mkdir()
        (agent_dir / "memory" / "daily").mkdir()
        (agent_dir / "skills").mkdir()

        # Имя переменной окружения
        env_var = f"{name.upper().replace('-', '_')}_BOT_TOKEN"

        # Сформировать allowed_users
        if allowed_users is None:
            # None = открытый доступ (пустой список = все могут)
            allowed_users_yaml = "  []  # открытый доступ\n"
        elif allowed_users:
            # FOUNDER + переданные пользователи
            lines = [f"  - {uid}\n" for uid in sorted(set(allowed_users))]
            allowed_users_yaml = "  - ${FOUNDER_TELEGRAM_ID}\n" + "".join(lines)
        else:
            # Пустой список = только FOUNDER
            allowed_users_yaml = "  - ${FOUNDER_TELEGRAM_ID}\n"

        # Записать agent.yaml
        yaml_content = AGENT_YAML_TEMPLATE.format(
            name=name,
            display_name=display_name,
            description=description,
            model=model,
            env_var=env_var,
            allowed_users_yaml=allowed_users_yaml,
        )
        (agent_dir / "agent.yaml").write_text(yaml_content, encoding="utf-8")

        # Записать SOUL.md
        if soul_md:
            (agent_dir / "SOUL.md").write_text(soul_md, encoding="utf-8")
        else:
            soul_content = SOUL_MD_TEMPLATE.format(
                display_name=display_name,
                description=description,
            )
            (agent_dir / "SOUL.md").write_text(soul_content, encoding="utf-8")

        # Добавить токен в .env
        self._add_env_var(env_var, bot_token)

        logger.info(f"Агент '{name}' создан в {agent_dir}")
        return agent_dir

    def list_agents(self) -> list[dict]:
        """
        Вернуть список агентов.

        Returns:
            Список словарей: name, display_name, model, token_set (bool)
        """
        if not self.agents_dir.exists():
            return []

        # Прочитать .env для проверки токенов
        env_vars = self._read_env_vars()

        agents = []
        for agent_yaml in sorted(self.agents_dir.glob("*/agent.yaml")):
            try:
                with open(agent_yaml, encoding="utf-8") as f:
                    raw = f.read()
                config = yaml.safe_load(raw)

                name = config.get("name", agent_yaml.parent.name)
                display_name = config.get("display_name", name)
                model = config.get("claude_model", "sonnet")

                # Определить имя env-переменной из bot_token
                token_ref = config.get("bot_token", "")
                token_set = False
                if isinstance(token_ref, str):
                    match = re.search(r"\$\{(\w+)\}", token_ref)
                    if match:
                        env_name = match.group(1)
                        token_set = bool(env_vars.get(env_name))
                    elif token_ref and "${" not in token_ref:
                        # Токен записан напрямую в yaml (не рекомендуется)
                        token_set = True

                agents.append({
                    "name": name,
                    "display_name": display_name,
                    "model": model,
                    "token_set": token_set,
                })
            except Exception as e:
                logger.warning(f"Ошибка чтения {agent_yaml}: {e}")

        return agents

    def validate_agent(self, agent_dir: Path) -> tuple[bool, list[str]]:
        """
        Проверить agent.yaml и SOUL.md.

        Returns:
            (ok, список ошибок)
        """
        errors = []
        yaml_path = agent_dir / "agent.yaml"
        soul_path = agent_dir / "SOUL.md"

        # agent.yaml существует
        if not yaml_path.exists():
            errors.append("agent.yaml не найден")
            return False, errors

        # Парсинг YAML
        try:
            with open(yaml_path, encoding="utf-8") as f:
                raw = f.read()
            config = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            errors.append(f"Ошибка парсинга YAML: {e}")
            return False, errors

        if not isinstance(config, dict):
            errors.append("agent.yaml должен быть словарём")
            return False, errors

        # Обязательные поля
        for field in ("name", "bot_token"):
            if field not in config:
                errors.append(f"Отсутствует обязательное поле: {field}")

        # Валидация имени
        name = config.get("name", "")
        if name and not AGENT_NAME_RE.match(name):
            errors.append(
                f"Имя '{name}' невалидно (только латиница, цифры, -, _)"
            )

        # Валидация модели
        model = config.get("claude_model", "sonnet")
        if model not in ("haiku", "sonnet", "opus"):
            errors.append(f"Неизвестная модель: {model}")

        # SOUL.md существует
        if not soul_path.exists():
            errors.append("SOUL.md не найден")

        # memory/ существует
        if not (agent_dir / "memory").exists():
            errors.append("Директория memory/ не найдена")

        return len(errors) == 0, errors

    def validate_all(self) -> dict[str, tuple[bool, list[str]]]:
        """Проверить всех агентов."""
        results = {}
        if not self.agents_dir.exists():
            return results

        for agent_dir in sorted(self.agents_dir.iterdir()):
            if agent_dir.is_dir() and (agent_dir / "agent.yaml").exists():
                results[agent_dir.name] = self.validate_agent(agent_dir)

        return results

    def _validate_create_params(
        self, name: str, bot_token: str, model: str
    ) -> list[str]:
        """Валидация параметров для create_agent."""
        errors = []

        if not name:
            errors.append("Имя агента не может быть пустым")
        elif not AGENT_NAME_RE.match(name):
            errors.append(
                "Имя агента: только латиница, цифры, дефис, подчёркивание. "
                "Начинается с буквы"
            )

        if not bot_token:
            errors.append("Токен бота не может быть пустым")
        elif not BOT_TOKEN_RE.match(bot_token):
            errors.append(
                "Невалидный токен бота. Формат: цифры:буквы "
                "(получить у @BotFather в Telegram)"
            )

        if model not in ("haiku", "sonnet", "opus"):
            errors.append(f"Неизвестная модель: {model}. Доступны: haiku, sonnet, opus")

        return errors

    def _read_env_vars(self) -> dict[str, str]:
        """Прочитать .env файл в словарь."""
        env = {}
        if not self.env_file.exists():
            return env
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
        return env

    def _add_env_var(self, var_name: str, value: str) -> None:
        """Добавить переменную в .env файл."""
        lines = []
        if self.env_file.exists():
            lines = self.env_file.read_text(encoding="utf-8").splitlines()

        # Проверить, не существует ли уже
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{var_name}="):
                lines[i] = f"{var_name}={value}"
                self.env_file.write_text(
                    "\n".join(lines) + "\n", encoding="utf-8"
                )
                return

        # Добавить новую переменную
        # Обеспечить пустую строку перед новой записью
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{var_name}={value}")

        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.env_file.chmod(0o600)  # Только владелец может читать
        logger.info(f"Добавлен {var_name} в .env")

        # Сразу установить в текущий процесс
        os.environ[var_name] = value
