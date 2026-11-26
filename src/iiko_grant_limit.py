"""Изменение лимита действующего подключения BB через личный Telegram владельца."""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from decimal import Decimal
from uuid import UUID

from .iiko_grant_term import term_label, valid_term


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
    confirm = len(args) == 3 and args[0] in ("confirm", "confirm-term")
    if len(args) != 2 and not confirm:
        raise ValueError("Команда: /iiko_limit ID_подключения сумма")
    gid = str(UUID(args[-2]))
    grant_path = root / (gid + ".json")
    pending_path = root / (gid + ".limit")
    term_change = args[0] == "confirm-term" if confirm else args[-1] == "until_revoked"
    if not confirm and not term_change:
        if not re.fullmatch(r"[0-9]{1,10}(?:[.,][0-9]{1,2})?", args[-1]):
            raise ValueError("Введите положительную сумму в рублях с точностью до копеек.")
        amount = Decimal(args[-1].replace(",", "."))
        if amount <= 0:
            raise ValueError("Сумма должна быть больше нуля.")
    if not confirm:
        catalog = {x["id"]: x["name"] for x in w.catalog()["organizations"]}

    with w.locked():
        stamp = now()
        grant = read_record(grant_path)
        if (grant["id"] != gid or grant["owner"] != owner or not valid_term(grant, stamp) or not grant["organizations"]
                or not set(grant["organizations"]) <= w.client.allowed):
            raise ValueError("Нет действующего разрешения для этих точек.")
        version = hashlib.sha256(json.dumps(grant, sort_keys=True).encode()).hexdigest()
        if confirm:
            pending = read_record(pending_path)
            if (pending.get("term", False) != term_change or pending["version"] != version or pending["until"] <= stamp
                    or not hmac.compare_digest(pending["nonce"].encode(), args[-1].encode())):
                raise ValueError("Подтверждение устарело. Запросите изменение лимита ещё раз.")
            # Погашаем подтверждение до записи: при сбое владелец сможет запросить новое.
            pending_path.unlink()
            if term_change:
                grant.update(expires=None, expiryMode="until_revoked",
                             termConfirmedBy=owner, termConfirmedAt=int(stamp))
                save(grant_path, grant)
                return (f"Доступ проекта {grant['project']} действует до отзыва. "
                        f"Лимит {grant['ceiling']} ₽ на накладную.\n"
                        f"Отозвать доступ: /iiko_review bb_revoke {gid}", None)
            grant["ceiling"] = pending["ceiling"]
            save(grant_path, grant)
            return f"Лимит проекта {grant['project']} установлен: {grant['ceiling']} ₽ на накладную.", None

        if not set(grant["organizations"]) <= catalog.keys():
            raise ValueError("Не удалось сверить точки в справочнике iiko.")
        shops = ", ".join(catalog[key] for key in grant["organizations"])
        nonce = secrets.token_urlsafe(16)
        until = stamp + 600
        if grant["expires"] is not None:
            until = min(until, grant["expires"])
        proposal = dict(version=version, nonce=nonce, until=until)
        if term_change:
            if grant.get("expiryMode") == "until_revoked":
                pending_path.unlink(missing_ok=True)
                return "Это подключение уже действует до отзыва.", None
            proposal["term"] = True
            text = (f"Разрешить проекту {grant['project']} проводить накладные до вашего отзыва?\n"
                    f"Сейчас доступ действует {term_label(grant)}. Новый срок: до отзыва.\n"
                    f"Точки: {shops}. Лимит: {grant['ceiling']} ₽ на накладную.\n"
                    f"Отозвать доступ можно командой /iiko_review bb_revoke {gid}")
            action = "confirm-term"
        else:
            if amount == Decimal(grant["ceiling"]):
                pending_path.unlink(missing_ok=True)
                return f"Действующий лимит уже равен {amount:.2f} ₽.", None
            proposal["ceiling"] = format(amount, ".2f")
            text = (f"Изменить лимит на одну накладную для {grant['project']}?\n"
                    f"{grant['ceiling']} ₽ → {amount:.2f} ₽\nТочки: {shops}.\n"
                    f"Доступ действует {term_label(grant)}.")
            action = "confirm"
        save(pending_path, proposal)
        return text + "\nДля подтверждения нажмите кнопку.", f"/iiko_limit {action} {gid} {nonce}"


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
