"""
CLI-визард для управления агентами.

Запуск:
    python -m src.cli create-agent    # Создать нового агента
    python -m src.cli list-agents     # Список всех агентов
    python -m src.cli validate        # Проверить конфиги
"""

import argparse
import sys
from pathlib import Path

from .agent_manager import AgentManager


def find_root() -> Path:
    """Найти корень проекта."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "agents").is_dir():
            return parent
    return Path(__file__).parent.parent


def cmd_list_agents(manager: AgentManager) -> None:
    """Таблица всех агентов."""
    agents = manager.list_agents()

    if not agents:
        print("Агенты не найдены.")
        return

    # Форматирование таблицы
    header = f"{'Имя':<15} {'Название':<25} {'Модель':<10} {'Токен':<10}"
    print(header)
    print("-" * len(header))

    for a in agents:
        token_status = "✓ задан" if a["token_set"] else "✗ нет"
        print(
            f"{a['name']:<15} {a['display_name']:<25} "
            f"{a['model']:<10} {token_status:<10}"
        )

    print(f"\nВсего: {len(agents)} агентов")


def cmd_validate(manager: AgentManager) -> None:
    """Валидация всех агентов."""
    results = manager.validate_all()

    if not results:
        print("Агенты не найдены.")
        return

    all_ok = True
    for name, (ok, errors) in results.items():
        if ok:
            print(f"  ✓ {name}")
        else:
            all_ok = False
            print(f"  ✗ {name}:")
            for err in errors:
                print(f"      - {err}")

    if all_ok:
        print(f"\nВсе {len(results)} агентов валидны.")
    else:
        print("\nЕсть ошибки. Исправь и запусти снова.")


def cmd_create_agent(manager: AgentManager) -> None:
    """Интерактивный визард создания агента."""
    print("=== Создание нового агента ===\n")

    # 1. Имя
    while True:
        name = input("Имя агента (латиницей, для папки): ").strip().lower()
        if not name:
            print("  Имя не может быть пустым.")
            continue
        if (manager.agents_dir / name).exists():
            print(f"  Агент '{name}' уже существует.")
            continue
        break

    # 2. Отображаемое имя
    display_name = input("Отображаемое имя (на русском): ").strip()
    if not display_name:
        display_name = name.title()

    # 3. Токен бота
    while True:
        bot_token = input("Токен бота (от @BotFather): ").strip()
        if not bot_token:
            print("  Токен не может быть пустым.")
            continue
        break

    # 4. Описание роли
    description = input("Описание роли (одно предложение): ").strip()
    if not description:
        description = f"AI-ассистент {display_name}"

    # 5. Модель
    model = input("Модель (haiku/sonnet/opus) [sonnet]: ").strip().lower()
    if model not in ("haiku", "sonnet", "opus"):
        model = "sonnet"

    # Подтверждение
    print(f"\n--- Проверь данные ---")
    print(f"  Имя:      {name}")
    print(f"  Название: {display_name}")
    print(f"  Токен:    {bot_token[:10]}...")
    print(f"  Роль:     {description}")
    print(f"  Модель:   {model}")

    confirm = input("\nВсё верно? (y/n) [y]: ").strip().lower()
    if confirm and confirm != "y":
        print("Отменено.")
        return

    # Создание
    try:
        agent_dir = manager.create_agent(
            name=name,
            display_name=display_name,
            bot_token=bot_token,
            description=description,
            model=model,
        )
        print(f"\n✓ Агент '{name}' создан в {agent_dir}")
        print(f"✓ Токен добавлен в .env как {name.upper().replace('-', '_')}_BOT_TOKEN")
        print(f"\nГотово! Перезапусти: systemctl restart my-claude-bot")
    except (ValueError, FileExistsError) as e:
        print(f"\n✗ Ошибка: {e}")
        sys.exit(1)


def main() -> None:
    """Точка входа CLI."""
    parser = argparse.ArgumentParser(
        description="My Claude Bot — управление агентами"
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("create-agent", help="Создать нового агента")
    subparsers.add_parser("list-agents", help="Список всех агентов")
    subparsers.add_parser("validate", help="Проверить конфиги всех агентов")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    root = find_root()
    manager = AgentManager(root)

    if args.command == "create-agent":
        cmd_create_agent(manager)
    elif args.command == "list-agents":
        cmd_list_agents(manager)
    elif args.command == "validate":
        cmd_validate(manager)


if __name__ == "__main__":
    main()
