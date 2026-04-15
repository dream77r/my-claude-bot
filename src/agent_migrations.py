"""
Runtime-миграции для Agent-конфигов.

Цель: при выкатке новой версии бота автоматически подключать встроенные
навыки/настройки существующим пользователям, не трогая их `agent.yaml`
(чтобы не ломать их кастомные mcp_servers, prompt и прочее).

Принцип: миграции **runtime-only** — модифицируют поля Agent в памяти,
а не пишут в файл. Идемпотентны: повторный запуск ничего не меняет.
Безопасны для пропуска: если триггер не выполнен (например, файл навыка
отсутствует) — миграция тихо пропускается.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# Список встроенных навыков, которые должны автоподключаться, если
# соответствующий файл лежит в <agent_dir>/skills/. Имя в списке — это
# stem-имя файла (без .md).
_BUILTIN_SKILLS = [
    "wiki-search",
]


def auto_register_builtin_skills(
    agent_dir: str,
    agent_name: str,
    skill_names: list[str],
) -> list[str]:
    """
    Дополнить список skill_names встроенными навыками, файлы которых
    физически присутствуют в <agent_dir>/skills/.

    Не модифицирует входной список — возвращает новый.
    """
    skills_dir = Path(agent_dir) / "skills"
    if not skills_dir.exists():
        return list(skill_names)

    result = list(skill_names)
    seen = {s.lower() for s in result}

    for builtin in _BUILTIN_SKILLS:
        if builtin.lower() in seen:
            continue
        skill_file = skills_dir / f"{builtin}.md"
        if not skill_file.exists():
            continue
        result.append(builtin)
        seen.add(builtin.lower())
        logger.info(
            f"Agent '{agent_name}': автоподключён встроенный навык "
            f"'{builtin}' (миграция runtime)"
        )

    return result
