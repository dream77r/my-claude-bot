"""
Dashboard & GitHub Backup commands mixin.

Команды Mini App / one-click setup / GitHub-бэкапов. Только master-агент
имеет дэшборд (единая точка входа во флот); бэкап-команды доступны
пользователям, но запуск вручную — только для founder.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from ..telegram_bridge import TelegramBridge  # noqa: F401

logger = logging.getLogger(__name__)


class DashboardCommandsMixin:
    """
    /dashboard, /setup_dashboard, /backup_link, /backup_now, /backup_status.

    Mixin предполагает наличие на self: `agent`, `_reply()`.
    """

    async def _cmd_dashboard(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Открыть Telegram Mini App с дэшбордом. Только master-агент —
        единая точка входа в общий fleet-cockpit."""
        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Дэшборд открывается только у master-агента (единая точка "
                "входа во флот). Напишите команду основному боту."
            )
            return
        url = os.environ.get("MINIAPP_URL", "").strip()
        if not url:
            await self._reply(
                update, context,
                "Дэшборд ещё не настроен. Администратор должен "
                "задать MINIAPP_URL в .env (требуется HTTPS).",
            )
            return
        if not url.startswith("https://"):
            await self._reply(
                update, context,
                "Дэшборд требует HTTPS-URL (ограничение Telegram WebApp).",
            )
            return

        separator = "&" if "?" in url else "?"
        launch_url = f"{url}{separator}origin_agent={self.agent.name}"
        button = InlineKeyboardButton(
            "Открыть дэшборд", web_app=WebAppInfo(url=launch_url)
        )
        await update.effective_message.reply_text(
            "Mini App агента «{}»:".format(self.agent.display_name),
            reply_markup=InlineKeyboardMarkup([[button]]),
        )

    async def _cmd_setup_dashboard(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """One-click настройка дэшборда: nginx + HTTPS + меню. Только master."""
        from ..miniapp import setup_flow

        if not self.agent.is_master:
            await self._reply(
                update, context,
                "Эта команда доступна только master-агенту."
            )
            return

        tokens = args.strip().split()
        if not tokens:
            await self._reply(
                update, context,
                "Использование: /setup_dashboard <domain> [email]\n"
                "Пример: /setup_dashboard bot.example.com you@example.com\n\n"
                "Что произойдёт:\n"
                "• Проверю DNS домена\n"
                "• Подберу свободный порт\n"
                "• Настрою nginx и выпущу HTTPS через Let's Encrypt\n"
                "• Обновлю .env и активирую кнопку меню Mini App\n"
                "• Перезапущу бота"
            )
            return

        try:
            domain = setup_flow.validate_domain(tokens[0])
        except ValueError as e:
            await self._reply(update, context, f"Невалидный домен: {e}")
            return

        email: str | None = None
        if len(tokens) >= 2:
            try:
                email = setup_flow.validate_email(tokens[1])
            except ValueError as e:
                await self._reply(update, context, f"Невалидный email: {e}")
                return

        if not await setup_flow.sudoers_ready():
            await self._reply(
                update, context,
                "Не могу запустить helper: sudo NOPASSWD не настроен.\n"
                "Включите один раз на сервере:\n"
                "    ./setup.sh --enable-dashboard\n"
                "и повторите /setup_dashboard."
            )
            return

        await self._reply(update, context, f"Проверяю DNS для {domain}…")
        try:
            ip = await setup_flow.check_dns(domain)
        except ValueError as e:
            await self._reply(
                update, context,
                f"{e}\n\nПроверьте что A-запись домена указывает на этот "
                "сервер и DNS успел распространиться."
            )
            return

        await self._reply(update, context, f"DNS OK → {ip}. Подбираю порт…")
        try:
            port = setup_flow.pick_free_port()
        except RuntimeError as e:
            await self._reply(update, context, f"Не нашёл свободный порт: {e}")
            return

        await self._reply(
            update, context,
            f"Порт {port}. Настраиваю nginx + certbot… (до 30 секунд)"
        )

        ok, output = await setup_flow.run_setup_helper(domain, port, email)
        if not ok:
            tail = "\n".join(output.splitlines()[-10:]) or "(нет вывода)"
            await self._reply(
                update, context,
                f"Helper завершился с ошибкой. Последние строки:\n\n{tail}"
            )
            return

        try:
            setup_flow.update_env_file(
                setup_flow.ENV_PATH,
                {
                    "HTTP_PORT": str(port),
                    "HTTP_HOST": "127.0.0.1",
                    "PUBLIC_BASE_URL": f"https://{domain}",
                    "MINIAPP_URL": f"https://{domain}/miniapp/",
                },
            )
        except OSError as e:
            await self._reply(update, context, f"Не смог записать .env: {e}")
            return

        menu_url = f"https://{domain}/miniapp/?origin_agent={self.agent.name}"
        try:
            await context.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Cockpit",
                    web_app=WebAppInfo(url=menu_url),
                )
            )
        except Exception as e:
            logger.warning(f"set_chat_menu_button failed: {e}")

        await self._reply(
            update, context,
            f"✅ Готово: https://{domain}/miniapp/\n"
            "Кнопка меню «Cockpit» активна. Перезапускаюсь…"
        )
        await setup_flow.trigger_restart_detached()

    # ── GitHub Backup commands ──

    async def _cmd_backup_link(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Показать ссылку на GitHub бэкап: /backup_link"""
        from ..github_sync import get_backup_url
        project_root = Path(self.agent.agent_dir).parent.parent
        url = get_backup_url(project_root)
        if not url:
            await self._reply(
                update, context,
                "GitHub Sync не настроен. Добавь GITHUB_SYNC_REPO в .env"
            )
            return
        await self._reply(update, context, f"Ссылка на бэкап:\n{url}")

    async def _cmd_backup_now(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Запустить бэкап прямо сейчас: /backup_now (только для founder)"""
        founder_id = int(os.environ.get("FOUNDER_TELEGRAM_ID", "0") or "0")
        user_id = update.effective_user.id if update.effective_user else 0
        if founder_id and user_id != founder_id:
            await self._reply(update, context, "Только владелец может запускать бэкап вручную.")
            return

        from ..github_sync import run_github_sync
        project_root = Path(self.agent.agent_dir).parent.parent

        await self._reply(update, context, "Запускаю бэкап... (займёт ~30 секунд)")
        result = await run_github_sync(project_root)

        if result["success"]:
            agents_str = ", ".join(result.get("agents", []))
            await self._reply(
                update, context,
                f"{result['message']}\n"
                f"Агенты: {agents_str}\n"
                f"Время: {result.get('timestamp', '')}\n"
                f"Ссылка: {result.get('repo_url', '')}"
            )
        else:
            await self._reply(update, context, f"Ошибка бэкапа: {result['message']}")

    async def _cmd_backup_status(
        self: TelegramBridge,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        args: str,
    ) -> None:
        """Показать статус последнего бэкапа: /backup_status"""
        from ..github_sync import get_last_sync_info, get_backup_url
        project_root = Path(self.agent.agent_dir).parent.parent
        info = get_last_sync_info(project_root)
        url = get_backup_url(project_root)

        if not info:
            repo_configured = bool(os.getenv("GITHUB_SYNC_REPO"))
            if not repo_configured:
                await self._reply(
                    update, context,
                    "GitHub Sync не настроен. Добавь GITHUB_SYNC_REPO и GITHUB_SYNC_TOKEN в .env"
                )
            else:
                await self._reply(
                    update, context,
                    "Бэкап ещё не выполнялся. Следующий — сегодня в 03:00 UTC.\n"
                    "Или запусти сейчас: /backup_now"
                )
            return

        agents_str = ", ".join(info.get("agents", []))
        await self._reply(
            update, context,
            f"Последний бэкап: {info.get('last_sync', 'неизвестно')}\n"
            f"Агенты: {agents_str}\n"
            f"Ссылка: {url or 'не настроено'}"
        )
