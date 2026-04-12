"""
CLI-визард для управления агентами.

Запуск:
    python -m src.cli create-agent           # Создать нового агента
    python -m src.cli list-agents            # Список всех агентов
    python -m src.cli validate               # Проверить конфиги

Skill Pool (маркетплейс):
    python -m src.cli pool refresh           # Клонировать/обновить пул
    python -m src.cli pool list              # Показать каталог пула
    python -m src.cli pool install <skill> <agent>   # Установить скилл
    python -m src.cli pool uninstall <skill> <agent> # Удалить скилл у агента
"""

import argparse
import sys
from pathlib import Path

from .agent_manager import AgentManager
from .skill_pool import make_pool_from_env, SkillPoolError


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


def cmd_pool_refresh(root: Path) -> None:
    """Клонировать/обновить публичный пул скиллов."""
    pool = make_pool_from_env(root)
    if pool is None:
        print("SKILL_POOL_URL не задан. Добавь в .env и попробуй снова.")
        sys.exit(1)
    try:
        pool.refresh()
        print(f"✓ Пул обновлён: {pool.repo_dir}")
        skills = pool.list_skills()
        print(f"  Доступно скиллов: {len(skills)}")
    except SkillPoolError as e:
        print(f"✗ Ошибка: {e}")
        sys.exit(1)


def cmd_pool_list(root: Path) -> None:
    """Показать каталог скиллов из пула."""
    pool = make_pool_from_env(root)
    if pool is None:
        print("SKILL_POOL_URL не задан.")
        sys.exit(1)
    if not pool.is_available():
        print("Пул не склонирован. Запусти: python -m src.cli pool refresh")
        sys.exit(1)

    try:
        skills = pool.list_skills()
    except SkillPoolError as e:
        print(f"✗ {e}")
        sys.exit(1)

    if not skills:
        print("В пуле нет опубликованных скиллов.")
        return

    for s in skills:
        tags = " ".join(f"#{t}" for t in s.tags) if s.tags else ""
        print(f"  {s.name:<25} v{s.version:<8} {s.description}")
        if tags:
            print(f"  {'':<25}        {tags}")
        if s.requires_memory:
            print(
                f"  {'':<25}        требует: {', '.join(s.requires_memory)}"
            )
    print(f"\nВсего: {len(skills)}")


def cmd_pool_install(root: Path, skill_name: str, agent_name: str) -> None:
    """Установить скилл в агента."""
    pool = make_pool_from_env(root)
    if pool is None:
        print("SKILL_POOL_URL не задан.")
        sys.exit(1)
    if not pool.is_available():
        print("Пул не склонирован. Запусти: python -m src.cli pool refresh")
        sys.exit(1)

    agent_dir = root / "agents" / agent_name
    if not agent_dir.exists():
        print(f"✗ Агент '{agent_name}' не найден в {agent_dir}")
        sys.exit(1)

    result = pool.install_skill(skill_name, agent_dir)
    if not result.ok:
        print(f"✗ {result.error}")
        sys.exit(1)

    print(f"✓ Скилл '{skill_name}' установлен в {result.installed_to}")
    if result.missing_memory:
        print(
            f"  Внимание: отсутствуют файлы памяти: "
            f"{', '.join(result.missing_memory)}"
        )
        print(
            "  Скилл работает, но пока не сможет читать из них. "
            "Создай через диалог с агентом."
        )


def cmd_pool_uninstall(root: Path, skill_name: str, agent_name: str) -> None:
    """Удалить скилл у агента."""
    pool = make_pool_from_env(root)
    if pool is None:
        print("SKILL_POOL_URL не задан.")
        sys.exit(1)

    agent_dir = root / "agents" / agent_name
    if not agent_dir.exists():
        print(f"✗ Агент '{agent_name}' не найден")
        sys.exit(1)

    if pool.uninstall_skill(skill_name, agent_dir):
        print(f"✓ Скилл '{skill_name}' удалён у агента '{agent_name}'")
    else:
        print(f"✗ Скилл '{skill_name}' не установлен у агента '{agent_name}'")
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

    # Pool subcommands
    pool_parser = subparsers.add_parser("pool", help="Управление пулом скиллов")
    pool_sub = pool_parser.add_subparsers(dest="pool_command")
    pool_sub.add_parser("refresh", help="Клонировать/обновить пул")
    pool_sub.add_parser("list", help="Показать каталог пула")
    p_install = pool_sub.add_parser("install", help="Установить скилл агенту")
    p_install.add_argument("skill", help="Имя скилла")
    p_install.add_argument("agent", help="Имя агента")
    p_uninstall = pool_sub.add_parser("uninstall", help="Удалить скилл у агента")
    p_uninstall.add_argument("skill", help="Имя скилла")
    p_uninstall.add_argument("agent", help="Имя агента")

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
    elif args.command == "pool":
        if not args.pool_command:
            pool_parser.print_help()
            sys.exit(1)
        if args.pool_command == "refresh":
            cmd_pool_refresh(root)
        elif args.pool_command == "list":
            cmd_pool_list(root)
        elif args.pool_command == "install":
            cmd_pool_install(root, args.skill, args.agent)
        elif args.pool_command == "uninstall":
            cmd_pool_uninstall(root, args.skill, args.agent)


if __name__ == "__main__":
    main()
