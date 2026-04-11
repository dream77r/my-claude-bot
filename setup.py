#!/usr/bin/env python3
"""
Интерактивная настройка My Claude Bot.

Запуск:
    python3 setup.py

Спрашивает токен бота и Telegram ID, записывает .env, запускает бота.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"


def bold(text):
    return f"\033[1m{text}\033[0m"


def green(text):
    return f"\033[32m{text}\033[0m"


def yellow(text):
    return f"\033[33m{text}\033[0m"


def red(text):
    return f"\033[31m{text}\033[0m"


def print_header():
    print()
    print(bold("=" * 50))
    print(bold("  My Claude Bot — Настройка"))
    print(bold("=" * 50))
    print()


def check_dependencies():
    """Проверить и установить зависимости."""
    try:
        import yaml
        import dotenv
        import telegram
        import claude_agent_sdk
        print(green("  Зависимости установлены"))
        return True
    except ImportError:
        print(yellow("  Устанавливаю зависимости..."))
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(red(f"  Ошибка установки: {result.stderr[:200]}"))
            return False
        print(green("  Зависимости установлены"))
        return True


def check_claude_cli():
    """Проверить что Claude CLI доступен."""
    result = subprocess.run(["which", "claude"], capture_output=True, text=True)
    if result.returncode != 0:
        print(red("  Claude CLI не найден!"))
        print("  Установи: https://docs.anthropic.com/en/docs/claude-code")
        return False
    print(green("  Claude CLI найден"))
    return True


def ask_bot_token() -> str:
    """Спросить токен бота."""
    print()
    print(bold("Шаг 1: Токен Telegram-бота"))
    print()
    print("  Если у тебя ещё нет бота:")
    print("  1. Открой Telegram, найди @BotFather")
    print("  2. Отправь /newbot")
    print("  3. Придумай имя и username")
    print("  4. Скопируй токен (вида 123456:ABC-DEF...)")
    print()

    while True:
        token = input(bold("  Вставь токен бота: ")).strip()
        if ":" in token and len(token) > 20:
            return token
        print(red("  Не похоже на токен. Формат: 123456:ABC-DEF..."))


def ask_telegram_id() -> str:
    """Спросить Telegram ID пользователя."""
    print()
    print(bold("Шаг 2: Твой Telegram ID"))
    print()
    print("  Как узнать свой ID:")
    print("  1. Открой Telegram, найди @userinfobot")
    print("  2. Отправь ему любое сообщение")
    print("  3. Он ответит твой ID (число)")
    print()

    while True:
        uid = input(bold("  Вставь свой Telegram ID: ")).strip()
        if uid.isdigit() and len(uid) >= 5:
            return uid
        print(red("  ID должен быть числом (минимум 5 цифр)"))


def ask_deepgram_key() -> str:
    """Спросить Deepgram API ключ (опционально)."""
    print()
    print(bold("Шаг 3: Deepgram API ключ (для голосовых сообщений)"))
    print()
    print("  Бот может распознавать голосовые через Deepgram API.")
    print("  Есть бесплатный тариф на $200 кредитов (~46 000 минут).")
    print()
    print("  Как получить:")
    print("  1. Зайди на https://console.deepgram.com/")
    print("  2. Зарегистрируйся и создай API Key")
    print()

    key = input(bold("  Вставь ключ (или Enter чтобы пропустить): ")).strip()
    if key:
        print(green("  Deepgram настроен — голосовые будут работать"))
    else:
        print(yellow("  Пропущено — голосовые можно настроить позже через .env"))
    return key


def write_env(token: str, user_id: str, deepgram_key: str = ""):
    """Записать .env файл."""
    content = f"""# My Claude Bot
ME_BOT_TOKEN={token}
FOUNDER_TELEGRAM_ID={user_id}
"""
    if deepgram_key:
        content += f"\n# Deepgram API для голосовых сообщений\nDEEPGRAM_API_KEY={deepgram_key}\n"
    ENV_FILE.write_text(content)
    ENV_FILE.chmod(0o600)  # Только владелец может читать (секреты)
    print()
    print(green("  .env файл создан (права: 600)"))


def print_ready(user_id: str):
    """Финальное сообщение."""
    print()
    print(bold("=" * 50))
    print(green(bold("  Всё готово!")))
    print(bold("=" * 50))
    print()
    print("  Сейчас бот запустится.")
    print("  Открой Telegram и напиши боту любое сообщение.")
    print()
    print(yellow("  Остановить: Ctrl+C"))
    print()


def main():
    os.chdir(ROOT)

    print_header()

    # Проверки
    print(bold("Проверяю окружение..."))
    if not check_claude_cli():
        sys.exit(1)
    if not check_dependencies():
        sys.exit(1)

    # Если .env уже есть — спросить перезаписать
    if ENV_FILE.exists():
        print()
        print(yellow(f"  .env уже существует"))
        answer = input("  Перенастроить? (y/n): ").strip().lower()
        if answer not in ("y", "yes", "д", "да"):
            print()
            print("  Запускаю с текущими настройками...")
            print()
            os.execv(sys.executable, [sys.executable, "-m", "src.main"])
            return

    # Интерактивная настройка
    token = ask_bot_token()
    user_id = ask_telegram_id()
    deepgram_key = ask_deepgram_key()
    write_env(token, user_id, deepgram_key)

    print_ready(user_id)

    # Запуск бота
    os.execv(sys.executable, [sys.executable, "-m", "src.main"])


if __name__ == "__main__":
    main()
