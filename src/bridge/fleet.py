"""
Fleet & Skill management commands mixin.

Команды управления флотом агентов (/agents, /create_agent, /clone_agent,
/set_access, /stop_agent, /start_agent), скиллами (/skills, /newskill,
/removeskill) и пулом скиллов (/poolskills, /installskill, /refreshpool).
Wizards для создания и клонирования агентов.

Mixin предполагает наличие на self: `agent`, `fleet_runtime`,
`_wizard_state`, `_get_skill_pool()`, `_reply()`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from ..status_message import StatusMessage

if TYPE_CHECKING:
    from ..telegram_bridge import TelegramBridge  # noqa: F401

logger = logging.getLogger(__name__)


class FleetCommandsMixin:
    """
    Команды управления флотом, скиллами, доступом + wizards.
    """

    # ── Agent Manager commands ──

    async def _cmd_agents(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Список всех агентов и их статус."""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        agents = self.fleet_runtime.manager.list_agents()
        if not agents:
            await self._reply(update, context, "Агенты не найдены.")
            return

        lines = ["Агенты:\n"]
        for a in agents:
            is_running = self.fleet_runtime.is_running(a["name"])
            if is_running:
                status = "🟢 запущен"
            elif a["token_set"]:
                status = "🔴 остановлен"
            else:
                status = "⚪ нет токена"

            lines.append(
                f"  {a['name']} — {a['display_name']}\n"
                f"    Модель: {a['model']} | {status}"
            )

        lines.append(f"\nВсего: {len(agents)}")
        await self._reply(update, context, "\n".join(lines))

    async def _cmd_create_agent(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Начать визард создания агента."""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        chat_id = update.effective_chat.id
        self._wizard_state[chat_id] = {"step": "name", "data": {}}

        await self._reply(
            update, context,
            "Создание нового агента.\n\n"
            "Шаг 1/6: Введи имя агента (латиницей, для папки).\n"
            "Пример: researcher, writer, support\n\n"
            "Отправь /cancel чтобы отменить."
        )

    async def _wizard_handle_input(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        """Обработать ввод пользователя в режиме визарда."""
        chat_id = update.effective_chat.id
        state = self._wizard_state.get(chat_id)
        if not state:
            return

        # Отмена
        if text.strip().lower() in ("/cancel", "отмена"):
            self._wizard_state.pop(chat_id, None)
            await self._reply(update, context, "Создание агента отменено.")
            return

        # Clone wizard (шаги clone_*)
        if state["step"].startswith("clone_"):
            await self._clone_wizard_handle(update, context, text)
            return

        step = state["step"]
        data = state["data"]

        if step == "name":
            name = text.strip().lower()
            # Валидация
            from ..agent_manager import AGENT_NAME_RE
            if not AGENT_NAME_RE.match(name):
                await self._reply(
                    update, context,
                    "Имя должно быть латиницей (a-z, 0-9, -, _), начинаться с буквы.\n"
                    "Попробуй ещё раз:"
                )
                return
            if (self.fleet_runtime.root / "agents" / name).exists():
                await self._reply(
                    update, context,
                    f"Агент '{name}' уже существует. Выбери другое имя:"
                )
                return
            data["name"] = name
            state["step"] = "display_name"
            await self._reply(
                update, context,
                f"Имя: {name}\n\n"
                "Шаг 2/6: Отображаемое имя (на русском).\n"
                "Пример: Исследователь, Копирайтер, Поддержка"
            )

        elif step == "display_name":
            data["display_name"] = text.strip()
            state["step"] = "token"
            await self._reply(
                update, context,
                f"Название: {data['display_name']}\n\n"
                "Шаг 3/6: Токен бота от @BotFather.\n"
                "Создай бота в Telegram через @BotFather и пришли токен."
            )

        elif step == "token":
            token = text.strip()
            from ..agent_manager import BOT_TOKEN_RE
            if not BOT_TOKEN_RE.match(token):
                await self._reply(
                    update, context,
                    "Невалидный токен. Формат: цифры:буквы\n"
                    "Получить: @BotFather → /newbot\n"
                    "Попробуй ещё раз:"
                )
                return
            data["token"] = token
            state["step"] = "description"
            await self._reply(
                update, context,
                "Шаг 4/6: Описание роли (одно предложение).\n"
                "Пример: AI-исследователь, помогает находить и анализировать информацию"
            )

        elif step == "description":
            data["description"] = text.strip()
            state["step"] = "model"
            await self._reply(
                update, context,
                f"Роль: {data['description']}\n\n"
                "Шаг 5/6: Модель Claude.\n"
                "Варианты: haiku (быстрая), sonnet (баланс), opus (максимум)\n"
                "Просто напиши название или нажми Enter для sonnet."
            )

        elif step == "model":
            model = text.strip().lower()
            if model not in ("haiku", "sonnet", "opus"):
                model = "sonnet"
            data["model"] = model

            state["step"] = "users"
            await self._reply(
                update, context,
                f"Модель: {model}\n\n"
                "Шаг 6/6: Для кого этот агент?\n\n"
                "Варианты:\n"
                "- Перешли мне сообщение от клиента — я возьму его ID автоматически\n"
                "- Введи Telegram ID вручную (число)\n"
                "- Напиши 'все' — бот будет доступен всем\n"
                "- Напиши 'я' — только для тебя"
            )

        elif step == "users":
            user_ids = []
            input_text = text.strip().lower()

            if input_text in ("я", "me", "i"):
                # Только текущий пользователь (owner)
                user_ids = []  # FOUNDER подставится автоматически
            elif input_text in ("все", "all", "любой", "everyone"):
                user_ids = []  # Пустой список = доступ для всех
                data["open_access"] = True
            else:
                # Попробовать парсить как числа (ID)
                for part in text.replace(",", " ").split():
                    part = part.strip()
                    if part.isdigit():
                        user_ids.append(int(part))

                # Проверить forwarded message
                fwd_origin = getattr(update.message, "forward_origin", None)
                if fwd_origin and getattr(fwd_origin, "type", "") == "user":
                    user_ids.append(fwd_origin.sender_user.id)

            data["allowed_users"] = user_ids

            # Описание доступа
            if data.get("open_access"):
                access_desc = "все (открытый доступ)"
            elif user_ids:
                access_desc = f"ты + {', '.join(str(uid) for uid in user_ids)}"
            else:
                access_desc = "только ты"

            # Показать подтверждение
            state["step"] = "confirm"
            await self._reply(
                update, context,
                "Проверь данные:\n\n"
                f"  Имя: {data['name']}\n"
                f"  Название: {data['display_name']}\n"
                f"  Токен: {data['token'][:10]}...\n"
                f"  Роль: {data['description']}\n"
                f"  Модель: {data['model']}\n"
                f"  Доступ: {access_desc}\n\n"
                "Создать? (да/нет)"
            )

        elif step == "confirm":
            answer = text.strip().lower()
            if answer in ("да", "yes", "y", "д"):
                self._wizard_state.pop(chat_id, None)
                await self._wizard_create(update, context, data)
            elif answer in ("нет", "no", "n", "н"):
                self._wizard_state.pop(chat_id, None)
                await self._reply(update, context, "Создание отменено.")
            else:
                await self._reply(update, context, "Напиши 'да' или 'нет':")

    async def _wizard_create(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        data: dict,
    ) -> None:
        """Финальный шаг визарда — создание агента и hot-reload."""
        chat_id = update.effective_chat.id

        try:
            # Если открытый доступ — пустой allowed_users в yaml
            allowed_users = data.get("allowed_users", []) or None
            if data.get("open_access"):
                allowed_users = None  # Пустой список = все могут

            self.fleet_runtime.manager.create_agent(
                name=data["name"],
                display_name=data["display_name"],
                bot_token=data["token"],
                description=data["description"],
                model=data["model"],
                allowed_users=allowed_users,
            )
        except (ValueError, FileExistsError) as e:
            await self._reply(update, context, f"Ошибка: {e}")
            return

        await self._reply(
            update, context,
            f"Агент '{data['name']}' создан. Запускаю..."
        )

        # Hot-reload
        ok, msg = await self.fleet_runtime.start_agent(data["name"])
        if ok:
            # Получить ссылку на нового бота
            invite_link = await self._get_bot_invite_link(data["token"])
            reply = (
                f"Готово! Агент '{data['display_name']}' запущен.\n\n"
                f"Ссылка для клиента:\n{invite_link}\n\n"
                f"Клиент нажмёт Start — и бот автоматически запомнит его ID."
            )
            await self._reply(update, context, reply)
        else:
            await self._reply(
                update, context,
                f"Агент создан, но не запустился: {msg}\n"
                "Попробуй /start_agent " + data["name"]
            )

    def _save_allowed_users(
        self: TelegramBridge, new_user_id: int, user_name: str
    ) -> None:
        """Добавить user ID в agent.yaml (persist)."""
        try:
            import yaml as _yaml
            yaml_path = Path(self.agent.config_path)
            with open(yaml_path, encoding="utf-8") as f:
                config = _yaml.safe_load(f.read())
            users = config.get("allowed_users", [])
            if isinstance(users, list) and new_user_id not in users:
                users.append(new_user_id)
                config["allowed_users"] = users
                with open(yaml_path, "w", encoding="utf-8") as f:
                    _yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                logger.info(
                    f"Auto-registered user {new_user_id} ({user_name}) "
                    f"for agent '{self.agent.name}'"
                )
        except Exception as e:
            logger.error(f"Failed to save allowed_users: {e}")

    async def _get_bot_invite_link(self: TelegramBridge, bot_token: str) -> str:
        """Получить invite link для бота по токену."""
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{bot_token}/getMe",
                    timeout=10,
                )
                data = resp.json()
                if data.get("ok"):
                    username = data["result"].get("username", "")
                    if username:
                        return f"https://t.me/{username}"
        except Exception:
            pass
        return "(не удалось получить ссылку — найди бота в Telegram вручную)"

    async def _cmd_clone_agent(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Клонировать агента: /clone_agent source_name"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        source_name = args.strip()
        if not source_name:
            # Показать список доступных агентов для клонирования
            agents = self.fleet_runtime.manager.list_agents()
            if not agents:
                await self._reply(update, context, "Нет агентов для клонирования.")
                return
            lines = ["Какого агента клонировать?\n"]
            for a in agents:
                lines.append(f"  /clone_agent {a['name']}  — {a['display_name']}")
            await self._reply(update, context, "\n".join(lines))
            return

        # Проверить что источник существует
        source_dir = self.fleet_runtime.root / "agents" / source_name
        if not source_dir.exists():
            await self._reply(update, context, f"Агент '{source_name}' не найден.")
            return

        # Запустить визард клонирования (сокращённый: имя, токен, доступ)
        chat_id = update.effective_chat.id
        self._wizard_state[chat_id] = {
            "step": "clone_name",
            "data": {"clone_from": source_name},
        }
        await self._reply(
            update, context,
            f"Клонирую агента '{source_name}'.\n"
            "Скопирую: SOUL.md, скиллы, модель, настройки dream/heartbeat.\n\n"
            "Шаг 1/4: Имя нового агента (латиницей).\n"
            "Отправь /cancel чтобы отменить."
        )

    async def _clone_wizard_handle(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> bool:
        """Обработать ввод визарда клонирования. Возвращает True если обработал."""
        chat_id = update.effective_chat.id
        state = self._wizard_state.get(chat_id)
        if not state or not state["step"].startswith("clone_"):
            return False

        step = state["step"]
        data = state["data"]

        if step == "clone_name":
            name = text.strip().lower()
            from ..agent_manager import AGENT_NAME_RE
            if not AGENT_NAME_RE.match(name):
                await self._reply(update, context, "Имя должно быть латиницей. Попробуй ещё:")
                return True
            if (self.fleet_runtime.root / "agents" / name).exists():
                await self._reply(update, context, f"'{name}' уже существует. Другое имя:")
                return True
            data["name"] = name
            state["step"] = "clone_display"
            await self._reply(
                update, context,
                f"Имя: {name}\n\nШаг 2/4: Отображаемое имя (на русском)."
            )

        elif step == "clone_display":
            data["display_name"] = text.strip()
            state["step"] = "clone_token"
            await self._reply(
                update, context,
                f"Название: {data['display_name']}\n\n"
                "Шаг 3/4: Токен бота от @BotFather."
            )

        elif step == "clone_token":
            token = text.strip()
            from ..agent_manager import BOT_TOKEN_RE
            if not BOT_TOKEN_RE.match(token):
                await self._reply(update, context, "Невалидный токен. Попробуй ещё:")
                return True
            data["token"] = token
            state["step"] = "clone_users"
            await self._reply(
                update, context,
                "Шаг 4/4: Для кого этот агент?\n\n"
                "- Перешли сообщение от клиента — ID автоматически\n"
                "- Введи Telegram ID (число)\n"
                "- 'все' — открытый доступ\n"
                "- 'я' — только ты"
            )

        elif step == "clone_users":
            user_ids = []
            input_text = text.strip().lower()

            if input_text in ("все", "all"):
                data["open_access"] = True
            elif input_text not in ("я", "me", "i"):
                for part in text.replace(",", " ").split():
                    if part.strip().isdigit():
                        user_ids.append(int(part.strip()))
                fwd_origin = getattr(update.message, "forward_origin", None)
                if fwd_origin and getattr(fwd_origin, "type", "") == "user":
                    user_ids.append(fwd_origin.sender_user.id)

            data["allowed_users"] = user_ids
            self._wizard_state.pop(chat_id, None)

            # Создать клон
            try:
                allowed = None if data.get("open_access") else (user_ids or [])
                self.fleet_runtime.manager.clone_agent(
                    source_name=data["clone_from"],
                    new_name=data["name"],
                    new_display_name=data["display_name"],
                    new_bot_token=data["token"],
                    allowed_users=allowed,
                )
            except (ValueError, FileExistsError) as e:
                await self._reply(update, context, f"Ошибка: {e}")
                return True

            await self._reply(
                update, context,
                f"Агент '{data['name']}' клонирован из '{data['clone_from']}'. Запускаю..."
            )

            ok, msg = await self.fleet_runtime.start_agent(data["name"])
            if ok:
                invite_link = await self._get_bot_invite_link(data["token"])
                await self._reply(
                    update, context,
                    f"Готово! '{data['display_name']}' запущен.\n\n"
                    f"Ссылка для клиента:\n{invite_link}\n\n"
                    f"Клиент нажмёт Start — бот запомнит его ID."
                )
            else:
                await self._reply(
                    update, context,
                    f"Клонирован, но не запустился: {msg}\n"
                    f"Попробуй /start_agent {data['name']}"
                )

        return True

    async def _cmd_set_access(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Управление доступом: /set_access agent_name [user_id | forward | all | lock]"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        parts = args.strip().split(None, 1)
        if not parts:
            # Показать справку
            agents = self.fleet_runtime.manager.list_agents()
            lines = [
                "Управление доступом к агентам.\n",
                "Использование:",
                "  /set_access имя_агента — показать текущий доступ",
                "  /set_access имя_агента 123456 — добавить user ID",
                "  /set_access имя_агента all — открыть для всех",
                "  /set_access имя_агента lock — только владелец",
                "  Или перешли сообщение от клиента с командой:\n"
                "  /set_access имя_агента + переслать сообщение\n",
            ]
            if agents:
                lines.append("Агенты:")
                for a in agents:
                    lines.append(f"  {a['name']} — {a['display_name']}")
            await self._reply(update, context, "\n".join(lines))
            return

        agent_name = parts[0]
        agent_yaml_path = self.fleet_runtime.root / "agents" / agent_name / "agent.yaml"

        if not agent_yaml_path.exists():
            await self._reply(update, context, f"Агент '{agent_name}' не найден.")
            return

        # Прочитать текущий конфиг
        import yaml as _yaml
        with open(agent_yaml_path, encoding="utf-8") as f:
            raw = f.read()
        config = _yaml.safe_load(raw)
        current_users = config.get("allowed_users", [])

        # Только показать текущий доступ
        if len(parts) == 1:
            # Проверить forwarded message
            fwd_origin = getattr(update.message, "forward_origin", None)
            if fwd_origin and getattr(fwd_origin, "type", "") == "user":
                # Добавить ID из пересланного сообщения
                fwd_user = fwd_origin.sender_user
                new_id = fwd_user.id
                if current_users and new_id not in current_users:
                    current_users.append(new_id)
                    config["allowed_users"] = current_users
                    with open(agent_yaml_path, "w", encoding="utf-8") as f:
                        _yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                    await self._reply(
                        update, context,
                        f"Добавлен доступ: {new_id} ({fwd_user.first_name or ''}) → '{agent_name}'\n"
                        f"Перезапусти агента: /stop_agent {agent_name} → /start_agent {agent_name}"
                    )
                    return
                elif not current_users:
                    await self._reply(update, context, f"'{agent_name}' уже открыт для всех.")
                    return
                else:
                    await self._reply(update, context, f"ID {new_id} уже в списке доступа.")
                    return

            # Показать текущий доступ + ссылку на бота
            if not current_users:
                access = "открытый (все могут писать)"
            else:
                import os
                founder_id = int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
                clients = [uid for uid in current_users if uid != founder_id]
                if clients:
                    access = "привязан к: " + ", ".join(str(uid) for uid in clients)
                else:
                    access = "только владелец (клиент не привязан)"

            # Получить ссылку на бота
            bot_token = config.get("bot_token", "")
            # Раскрыть ${VAR}
            if "${" in bot_token:
                import os as _os
                bot_token = _os.path.expandvars(bot_token)

            link = ""
            if bot_token and "${" not in bot_token:
                link = await self._get_bot_invite_link(bot_token)

            lines = [
                f"Агент: {agent_name}",
                f"Доступ: {access}",
            ]
            if link and not link.startswith("("):
                lines.append(f"\nСсылка для клиента:\n{link}")
                lines.append("Первый кто нажмёт Start — получит доступ.")
            lines.append(f"\nКоманды:")
            lines.append(f"  /set_access {agent_name} lock — сбросить привязку")
            lines.append(f"  /set_access {agent_name} all — открыть для всех")

            await self._reply(update, context, "\n".join(lines))
            return

        action = parts[1].strip()

        # Добавить user ID
        if action.isdigit():
            new_id = int(action)
            if not current_users:
                current_users = [new_id]
            elif new_id not in current_users:
                current_users.append(new_id)
            else:
                await self._reply(update, context, f"ID {new_id} уже в списке.")
                return
            config["allowed_users"] = current_users

        # Открыть для всех
        elif action in ("all", "все", "open"):
            config["allowed_users"] = []

        # Только владелец
        elif action in ("lock", "закрыть", "only_me"):
            import os
            founder_id = os.environ.get("FOUNDER_TELEGRAM_ID", "")
            config["allowed_users"] = [int(founder_id)] if founder_id.isdigit() else []

        else:
            await self._reply(
                update, context,
                f"Неизвестное действие: {action}\n"
                "Варианты: число (ID), all, lock"
            )
            return

        # Сохранить
        with open(agent_yaml_path, "w", encoding="utf-8") as f:
            _yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        if not config["allowed_users"]:
            result = "открытый доступ (все)"
        else:
            result = ", ".join(str(uid) for uid in config["allowed_users"])

        await self._reply(
            update, context,
            f"Доступ к '{agent_name}' обновлён: {result}\n"
            f"Перезапусти: /stop_agent {agent_name} → /start_agent {agent_name}"
        )

    async def _cmd_stop_agent(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Остановить агента: /stop_agent name"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        name = args.strip()
        if not name:
            await self._reply(
                update, context,
                "Укажи имя агента: /stop_agent <имя>\n"
                "Список: /agents"
            )
            return

        # Нельзя остановить самого себя
        if name == self.agent.name:
            await self._reply(update, context, "Нельзя остановить самого себя.")
            return

        ok, msg = await self.fleet_runtime.stop_agent(name)
        await self._reply(update, context, msg)

    async def _cmd_start_agent(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Запустить агента: /start_agent name"""
        if not self.fleet_runtime:
            await self._reply(update, context, "Agent Manager недоступен.")
            return

        name = args.strip()
        if not name:
            await self._reply(
                update, context,
                "Укажи имя агента: /start_agent <имя>\n"
                "Список: /agents"
            )
            return

        ok, msg = await self.fleet_runtime.start_agent(name)
        await self._reply(update, context, msg)

    # ── Skill Creator commands ──

    async def _cmd_skills(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Показать список скиллов агента: /skills [agent_name]"""
        from ..skill_creator import list_skills, get_all_agent_dirs

        target_name = args.strip() if args.strip() else self.agent.name

        # Найти директорию целевого агента
        if target_name == self.agent.name:
            agent_dir = self.agent.agent_dir
        elif self.fleet_runtime and target_name in self.fleet_runtime.agents:
            agent_dir = self.fleet_runtime.agents[target_name].agent_dir
        else:
            # Попробовать найти по файловой системе
            agents = get_all_agent_dirs(
                str(Path(self.agent.agent_dir).parent.parent)
            )
            agent_dir = agents.get(target_name)

        if not agent_dir:
            await self._reply(
                update, context,
                f"Агент '{target_name}' не найден."
            )
            return

        skills = list_skills(agent_dir)
        if not skills:
            await self._reply(
                update, context,
                f"У агента '{target_name}' нет скиллов.\n"
                f"Создай: /newskill описание скилла"
            )
            return

        lines = [f"Скиллы агента '{target_name}':"]
        for s in skills:
            always_tag = " [always]" if s["always"] else ""
            lines.append(f"  - {s['name']}{always_tag}: {s['description']}")
        lines.append(f"\nВсего: {len(skills)}")

        await self._reply(update, context, "\n".join(lines))

    async def _cmd_newskill(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Создать новый скилл: /newskill описание"""
        # Только master может создавать скиллы
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Создание скиллов доступно только master-агенту."
            )
            return

        description = args.strip()
        if not description:
            await self._reply(
                update, context,
                "Опиши скилл: /newskill <описание>\n\n"
                "Примеры:\n"
                "  /newskill анализ конкурентов — исследование и сравнение\n"
                "  /newskill ежедневный отчёт по задачам команды\n"
                "  /newskill генерация SQL запросов из текстового описания"
            )
            return

        # Определить целевого агента
        # Формат: /newskill @agent_name описание
        # или просто /newskill описание (создаётся для текущего агента)
        target_name = self.agent.name
        target_dir = self.agent.agent_dir

        if description.startswith("@"):
            parts = description.split(maxsplit=1)
            candidate = parts[0][1:]  # убрать @
            if self.fleet_runtime and candidate in self.fleet_runtime.agents:
                target_name = candidate
                target_dir = self.fleet_runtime.agents[candidate].agent_dir
                description = parts[1] if len(parts) > 1 else ""
                if not description:
                    await self._reply(
                        update, context,
                        f"Укажи описание скилла для @{target_name}:\n"
                        f"/newskill @{target_name} <описание>"
                    )
                    return

        from ..skill_creator import create_skill

        chat_id = update.effective_chat.id
        status = StatusMessage(chat_id, context)
        await status.show("Генерирую скилл...")

        try:
            # Определить роль целевого агента
            if self.fleet_runtime and target_name in self.fleet_runtime.agents:
                target_role = self.fleet_runtime.agents[target_name].role
            else:
                target_role = "worker"

            ok, message = await create_skill(
                user_request=description,
                agent_dir=target_dir,
                agent_name=target_name,
                agent_role=target_role,
                model="sonnet",
            )

            await status.cleanup()
            await self._reply(update, context, message)

        except Exception as e:
            await status.cleanup()
            logger.error(f"NewSkill command error: {e}")
            await self._reply(
                update, context,
                f"Ошибка создания скилла: {e}"
            )

    async def _cmd_removeskill(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Удалить скилл: /removeskill skill_name [@agent_name]"""
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Удаление скиллов доступно только master-агенту."
            )
            return

        parts = args.strip().split()
        if not parts:
            await self._reply(
                update, context,
                "Укажи имя скилла: /removeskill <skill_name> [@agent_name]\n"
                "Список скиллов: /skills"
            )
            return

        skill_name = parts[0]

        # Определить целевого агента
        target_dir = self.agent.agent_dir
        target_name = self.agent.name
        if len(parts) > 1 and parts[1].startswith("@"):
            candidate = parts[1][1:]
            if self.fleet_runtime and candidate in self.fleet_runtime.agents:
                target_name = candidate
                target_dir = self.fleet_runtime.agents[candidate].agent_dir
            else:
                await self._reply(
                    update, context,
                    f"Агент '{candidate}' не найден."
                )
                return

        from ..skill_creator import remove_skill

        ok = remove_skill(skill_name, target_dir)
        if ok:
            await self._reply(
                update, context,
                f"Скилл '{skill_name}' удалён у агента '{target_name}'."
            )
        else:
            await self._reply(
                update, context,
                f"Скилл '{skill_name}' не найден у агента '{target_name}'.\n"
                f"Список: /skills {target_name}"
            )

    # ── Skill Pool commands (маркетплейс) ──

    async def _cmd_poolskills(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Показать каталог скиллов из пула: /poolskills"""
        pool = self._get_skill_pool()
        if pool is None:
            await self._reply(
                update, context,
                "Пул скиллов не настроен.\n"
                "Задай SKILL_POOL_URL в .env (см .env.example) "
                "и перезапусти бота."
            )
            return

        # Автоматически обновляем пул если его ещё нет на диске
        if not pool.is_available():
            try:
                pool.refresh()
            except Exception as e:
                await self._reply(
                    update, context,
                    f"Ошибка при клонировании пула: {e}\n"
                    f"Проверь SKILL_POOL_URL и доступность репо."
                )
                return

        try:
            skills = pool.list_skills()
        except Exception as e:
            await self._reply(
                update, context,
                f"Ошибка чтения manifest.json: {e}"
            )
            return

        if not skills:
            await self._reply(
                update, context,
                "В пуле пока нет опубликованных скиллов."
            )
            return

        lines = ["Каталог скиллов из пула:\n"]
        for s in skills:
            tags = " ".join(f"#{t}" for t in s.tags) if s.tags else ""
            mem_note = ""
            if s.requires_memory:
                mem_note = (
                    f"\n    требует память: {', '.join(s.requires_memory)}"
                )

            # Маркеры типа и скриптов перед именем
            type_mark = "📦" if s.type == "bundle" else "📄"
            scripts_mark = " ⚠️ скрипты" if s.has_scripts else ""

            lines.append(
                f"• {type_mark} *{s.name}* v{s.version}{scripts_mark} — "
                f"{s.description}{mem_note}"
            )
            if tags:
                lines.append(f"    {tags}")

        # Легенда для пользователя
        lines.append(
            f"\nВсего: {len(skills)}"
            f"\n📄 — single-file скилл (только markdown)"
            f"\n📦 — bundle (директория с доп. файлами)"
            f"\n⚠️ скрипты — содержит исполняемый код (Python/Bash/JS)"
            f"\n\nУстановить: /installskill <имя> [@agent]"
        )
        await self._reply(update, context, "\n".join(lines))

    async def _cmd_installskill(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Установить скилл из пула: /installskill <имя> [@agent]"""
        # Только master может устанавливать скиллы
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Установка скиллов доступна только master-агенту."
            )
            return

        parts = args.strip().split()
        if not parts:
            await self._reply(
                update, context,
                "Укажи имя скилла: /installskill <имя> [@agent]\n"
                "Каталог: /poolskills"
            )
            return

        skill_name = parts[0]

        # Определить целевого агента
        target_name = self.agent.name
        target_dir = Path(self.agent.agent_dir)
        if len(parts) > 1 and parts[1].startswith("@"):
            candidate = parts[1][1:]
            if self.fleet_runtime and candidate in self.fleet_runtime.agents:
                target_name = candidate
                target_dir = Path(
                    self.fleet_runtime.agents[candidate].agent_dir
                )
            else:
                await self._reply(
                    update, context,
                    f"Агент '{candidate}' не найден."
                )
                return

        pool = self._get_skill_pool()
        if pool is None:
            await self._reply(
                update, context,
                "Пул скиллов не настроен (SKILL_POOL_URL не задан)."
            )
            return

        if not pool.is_available():
            try:
                pool.refresh()
            except Exception as e:
                await self._reply(
                    update, context,
                    f"Ошибка при обновлении пула: {e}"
                )
                return

        result = pool.install_skill(skill_name, target_dir)

        if not result.ok:
            await self._reply(
                update, context,
                f"Не удалось установить '{skill_name}': {result.error}"
            )
            return

        msg_lines = [
            f"Скилл '{skill_name}' установлен агенту '{target_name}'.",
            f"Путь: {result.installed_to}",
        ]

        if result.has_scripts:
            msg_lines.append(
                "\n⚠️ Скилл содержит исполняемые скрипты (Python/Bash/JS). "
                "Они скопированы вместе со скиллом и могут запускаться "
                "Claude Agent SDK по запросу. Убедись что доверяешь автору "
                "перед реальным использованием."
            )

        if result.missing_memory:
            msg_lines.append(
                f"\nСкилл декларирует файлы памяти, которых пока нет у агента:\n"
                + "\n".join(f"  - {m}" for m in result.missing_memory)
                + "\n\nСкилл будет работать, но пока не сможет читать из них. "
                  "Создай эти файлы через обычный диалог с агентом — "
                  "он сам их заполнит когда ты ответишь на его вопросы."
            )

        await self._reply(update, context, "\n".join(msg_lines))

    async def _cmd_refreshpool(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Обновить кэш пула скиллов: /refreshpool"""
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Обновление пула доступно только master-агенту."
            )
            return

        pool = self._get_skill_pool()
        if pool is None:
            await self._reply(
                update, context,
                "Пул скиллов не настроен (SKILL_POOL_URL не задан)."
            )
            return

        try:
            pool.refresh()
        except Exception as e:
            await self._reply(
                update, context,
                f"Ошибка обновления пула: {e}"
            )
            return

        try:
            skills = pool.list_skills()
            await self._reply(
                update, context,
                f"Пул обновлён. Доступно скиллов: {len(skills)}.\n"
                f"Каталог: /poolskills"
            )
        except Exception as e:
            await self._reply(
                update, context,
                f"Пул склонирован, но manifest.json не читается: {e}"
            )
