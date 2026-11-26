"""Изменение лимита действующего подключения BB через личный Telegram владельца."""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID


def private_owner(update, founder, allowed):
    msg, user, chat = update.message, update.effective_user, update.effective_chat
    return bool(founder and msg and user and chat and not user.is_bot
                and founder == user.id == chat.id and chat.type == "private"
                and msg.from_user and msg.from_user.id == user.id and allowed(user.id)
                and not any(getattr(msg, key, None) for key in
                            ("forward_origin", "via_bot", "sender_chat", "business_connection_id")))


def read_record(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError("Подключение или запрос подтверждения не найден.") from None


def change_limit(w, root, save, owner, args, now=time.time):
    confirm = len(args) == 3 and args[0] == "confirm"
    if len(args) != 2 and not confirm:
        raise ValueError("Команда: /iiko_limit ID_подключения сумма")
    gid = str(UUID(args[-2]))
    grant_path = root / (gid + ".json")
    pending_path = root / (gid + ".limit")
    if not confirm:
        if not re.fullmatch(r"[0-9]{1,10}(?:[.,][0-9]{1,2})?", args[-1]):
            raise ValueError("Введите положительную сумму в рублях с точностью до копеек.")
        amount = Decimal(args[-1].replace(",", "."))
        if amount <= 0:
            raise ValueError("Сумма должна быть больше нуля.")
        catalog = {x["id"]: x["name"] for x in w.catalog()["organizations"]}

    with w.locked():
        stamp = now()
        grant = read_record(grant_path)
        if (grant["id"] != gid or grant["owner"] != owner or grant.get("revoked")
                or grant["expires"] <= stamp or not grant["organizations"]
                or not set(grant["organizations"]) <= w.client.allowed):
            raise ValueError("Нет действующего разрешения для этих точек.")
        version = hashlib.sha256(json.dumps(grant, sort_keys=True).encode()).hexdigest()
        if confirm:
            pending = read_record(pending_path)
            if (pending["version"] != version or pending["until"] <= stamp
                    or not hmac.compare_digest(pending["nonce"].encode(), args[-1].encode())):
                raise ValueError("Подтверждение устарело. Запросите изменение лимита ещё раз.")
            # Погашаем подтверждение до записи: при сбое владелец сможет запросить новое.
            pending_path.unlink()
            grant["ceiling"] = pending["ceiling"]
            save(grant_path, grant)
            return f"Лимит проекта {grant['project']} установлен: {grant['ceiling']} ₽ на накладную.", None

        if not set(grant["organizations"]) <= catalog.keys():
            raise ValueError("Не удалось сверить точки в справочнике iiko.")
        if amount == Decimal(grant["ceiling"]):
            pending_path.unlink(missing_ok=True)
            return f"Действующий лимит уже равен {amount:.2f} ₽.", None
        nonce = secrets.token_urlsafe(16)
        save(pending_path, dict(version=version, ceiling=format(amount, ".2f"), nonce=nonce,
                                until=min(stamp + 600, grant["expires"])))
        shops = ", ".join(catalog[key] for key in grant["organizations"])
        end = datetime.fromtimestamp(grant["expires"], timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        return (f"Изменить лимит на одну накладную для {grant['project']}?\n"
                f"{grant['ceiling']} ₽ → {amount:.2f} ₽\nТочки: {shops}.\n"
                f"Доступ действует до {end}. Для подтверждения нажмите кнопку.",
                f"/iiko_limit confirm {gid} {nonce}")


def run_command(owner, args):
    # Этот адаптер установлен только у ботов, подключённых к iiko.
    from .iiko_bb_api import store, workflow
    from .iiko_connector import save_json
    with contextmanager(workflow)() as w:
        return change_limit(w, store(w).root, save_json, owner, args)


async def handle_limit(bridge, update, context, args):
    from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
    from .miniapp.auth import _founder_id

    if not private_owner(update, _founder_id(), bridge.agent.is_user_allowed):
        return
    try:
        text, confirm = await asyncio.to_thread(run_command, update.effective_user.id, args.split())
    except ValueError as exc:
        text, confirm = str(exc), None
    except (ImportError, OSError):
        text, confirm = "Сервис изменения лимита временно недоступен.", None
    keyboard = (ReplyKeyboardMarkup([[confirm]], resize_keyboard=True, one_time_keyboard=True)
                if confirm else ReplyKeyboardRemove())
    await update.message.reply_text(text, reply_markup=keyboard)
