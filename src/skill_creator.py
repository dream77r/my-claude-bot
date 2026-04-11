"""
SkillCreator — динамическое создание скиллов через оркестратор.

Только master-агент может создавать скиллы (для себя и worker-ов).
Worker-агенты не имеют доступа к созданию скиллов (sandbox изоляция).

Потоки:
1. Пользователь → /newskill описание → master генерирует → install
2. SkillAdvisor suggestion → master одобряет → create_from_suggestion
3. Программный вызов → create_skill() напрямую

Скилл = markdown с YAML frontmatter в agents/{name}/skills/{skill_name}.md
"""

import json
import logging
import re
from pathlib import Path

import yaml

from . import memory
from .dream import _call_claude_simple, _extract_json

logger = logging.getLogger(__name__)

# Промпт для генерации скилла через Claude
_GENERATION_PROMPT = """\
Ты — генератор скиллов для AI-агента. Скилл — это markdown-инструкция \
с YAML frontmatter, которая учит агента выполнять определённый тип задач.

## Запрос пользователя
{user_request}

## Текущие скиллы агента (не дублируй)
{existing_skills}

## Профиль агента
Имя: {agent_name}
Роль: {agent_role}

## Формат ответа (строго JSON)

```json
{{
  "name": "skill-name-kebab-case",
  "skill_content": "---\\ndescription: \\"Краткое описание скилла\\"\\n\
requirements:\\n  commands: []\\n  env: []\\nalways: false\\n---\\n\\n\
# Skill: Название\\n\\n## Когда активировать\\n...\\n\\n## Инструкции\\n\
1. ...\\n2. ...\\n\\n## Формат ответа\\n..."
}}
```

## Правила
- name: kebab-case, латиница, 2-4 слова
- description в frontmatter: краткое, на русском, до 80 символов
- requirements.commands: список CLI утилит если нужны (curl, git, etc.)
- requirements.env: список переменных окружения если нужны
- always: false (пользователь сам решит, делать ли always: true)
- Тело скилла: чёткие инструкции для Claude, формат ответа, примеры
- Пиши на русском (кроме name и технических терминов)
- Не дублируй существующие скиллы
"""


def _get_skills_dir(agent_dir: str) -> Path:
    """Получить путь к директории скиллов агента."""
    return Path(agent_dir) / "skills"


def list_skills(agent_dir: str) -> list[dict]:
    """
    Список установленных скиллов агента.

    Returns:
        [{name, description, always, path}, ...]
    """
    skills_dir = _get_skills_dir(agent_dir)
    if not skills_dir.exists():
        return []

    from .agent import Agent

    result = []
    for f in sorted(skills_dir.glob("*.md")):
        raw = f.read_text(encoding="utf-8")
        meta, _ = Agent.parse_skill_frontmatter(raw)
        result.append({
            "name": f.stem,
            "description": meta.get("description", "") if meta else "",
            "always": meta.get("always", False) if meta else False,
            "path": str(f),
        })
    return result


def _get_existing_skills_text(agent_dir: str) -> str:
    """Форматировать список скиллов для промпта."""
    skills = list_skills(agent_dir)
    if not skills:
        return "(скиллов нет)"
    return "\n".join(
        f"- {s['name']}: {s['description']}" for s in skills
    )


def validate_skill(
    skill_name: str,
    skill_content: str,
    agent_dir: str,
) -> tuple[bool, list[str]]:
    """
    Валидировать скилл перед установкой.

    Проверки:
    - Имя: kebab-case, латиница, без спецсимволов
    - Frontmatter: валидный YAML с description
    - Уникальность: нет дубликата по имени
    - Содержимое: не пустое, есть инструкции

    Returns:
        (ok, list[errors])
    """
    from .agent import Agent

    errors = []

    # Имя
    if not skill_name:
        errors.append("Имя скилла пустое")
    elif not re.match(r"^[a-z][a-z0-9-]{1,50}$", skill_name):
        errors.append(
            f"Невалидное имя '{skill_name}': "
            "используй kebab-case, латиницу, 2-50 символов"
        )

    # Дубликат
    existing = _get_skills_dir(agent_dir) / f"{skill_name}.md"
    if existing.exists():
        errors.append(f"Скилл '{skill_name}' уже существует")

    # Frontmatter
    meta, body = Agent.parse_skill_frontmatter(skill_content)
    if meta is None:
        errors.append("Нет YAML frontmatter (---...---)")
    else:
        if not meta.get("description"):
            errors.append("Отсутствует description в frontmatter")

    # Содержимое
    if not body or len(body.strip()) < 20:
        errors.append("Тело скилла слишком короткое (< 20 символов)")

    return len(errors) == 0, errors


def install_skill(
    skill_name: str,
    skill_content: str,
    agent_dir: str,
    commit: bool = True,
) -> Path:
    """
    Установить скилл: записать .md файл и git commit.

    Args:
        skill_name: имя скилла (kebab-case)
        skill_content: полный контент с frontmatter
        agent_dir: директория агента
        commit: делать ли git commit

    Returns:
        Path к созданному файлу
    """
    skills_dir = _get_skills_dir(agent_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)

    skill_path = skills_dir / f"{skill_name}.md"
    skill_path.write_text(skill_content, encoding="utf-8")

    logger.info(f"SkillCreator: установлен '{skill_name}' → {skill_path}")

    if commit:
        memory.git_commit(
            agent_dir,
            f"SkillCreator: add skill '{skill_name}'"
        )

    return skill_path


def remove_skill(skill_name: str, agent_dir: str) -> bool:
    """
    Удалить скилл.

    Returns:
        True если удалён, False если не найден
    """
    skill_path = _get_skills_dir(agent_dir) / f"{skill_name}.md"
    if not skill_path.exists():
        return False

    skill_path.unlink()
    logger.info(f"SkillCreator: удалён '{skill_name}'")

    memory.git_commit(
        agent_dir,
        f"SkillCreator: remove skill '{skill_name}'"
    )
    return True


async def generate_skill(
    user_request: str,
    agent_dir: str,
    agent_name: str = "",
    agent_role: str = "worker",
    model: str = "sonnet",
) -> tuple[str | None, str | None, str]:
    """
    Сгенерировать скилл через Claude.

    Args:
        user_request: описание скилла от пользователя
        agent_dir: директория целевого агента
        agent_name: имя агента
        agent_role: роль агента (master/worker)
        model: модель Claude для генерации

    Returns:
        (skill_name, skill_content, error_message)
        При успехе error_message = ""
    """
    existing = _get_existing_skills_text(agent_dir)

    prompt = _GENERATION_PROMPT.format(
        user_request=user_request,
        existing_skills=existing,
        agent_name=agent_name or "unknown",
        agent_role=agent_role,
    )

    try:
        response = await _call_claude_simple(prompt, model=model)
    except Exception as e:
        logger.error(f"SkillCreator generate error: {e}")
        return None, None, f"Ошибка вызова Claude: {e}"

    data = _extract_json(response)
    if not data:
        logger.warning("SkillCreator: не удалось извлечь JSON из ответа")
        return None, None, "Не удалось разобрать ответ Claude"

    skill_name = data.get("name", "")
    skill_content = data.get("skill_content", "")

    if not skill_name or not skill_content:
        return None, None, "Claude вернул пустое имя или содержимое скилла"

    # Unescape: Claude может вернуть \\n вместо \n в JSON
    skill_content = skill_content.replace("\\n", "\n")

    return skill_name, skill_content, ""


async def create_skill(
    user_request: str,
    agent_dir: str,
    agent_name: str = "",
    agent_role: str = "worker",
    model: str = "sonnet",
) -> tuple[bool, str]:
    """
    Полный цикл: генерация → валидация → установка.

    Returns:
        (ok, message для пользователя)
    """
    # 1. Генерация
    skill_name, skill_content, error = await generate_skill(
        user_request, agent_dir, agent_name, agent_role, model
    )
    if error:
        return False, f"Ошибка генерации: {error}"

    # 2. Валидация
    ok, errors = validate_skill(skill_name, skill_content, agent_dir)
    if not ok:
        return False, (
            f"Скилл '{skill_name}' не прошёл валидацию:\n"
            + "\n".join(f"- {e}" for e in errors)
        )

    # 3. Установка
    path = install_skill(skill_name, skill_content, agent_dir)

    return True, (
        f"Скилл '{skill_name}' создан и установлен для агента '{agent_name}'.\n"
        f"Путь: {path}\n"
        f"Скилл будет активен при следующем сообщении."
    )


def create_from_suggestion(
    suggestion: dict,
    agent_dir: str,
) -> tuple[bool, str]:
    """
    Создать скилл из предложения SkillAdvisor (синхронно, без LLM).

    Используется когда у нас уже есть структурированное предложение
    с name, description, capabilities.

    Args:
        suggestion: dict из SkillAdvisor (suggested_skill + pattern + examples)

    Returns:
        (ok, message)
    """
    skill_info = suggestion.get("suggested_skill", {})
    skill_name = skill_info.get("name", "")
    title = skill_info.get("title", skill_name)
    description = skill_info.get("description", "")
    capabilities = skill_info.get("capabilities", [])
    pattern = suggestion.get("pattern", "")
    examples = suggestion.get("examples", [])

    if not skill_name:
        return False, "Нет имени скилла в предложении"

    # Сформировать markdown
    capabilities_text = "\n".join(f"- {c}" for c in capabilities)
    examples_text = "\n".join(f"- \"{e}\"" for e in examples[:5])

    content = (
        f"---\n"
        f"description: \"{description}\"\n"
        f"requirements:\n"
        f"  commands: []\n"
        f"  env: []\n"
        f"always: false\n"
        f"---\n\n"
        f"# Skill: {title}\n\n"
        f"## Когда активировать\n"
        f"{pattern}\n\n"
        f"## Возможности\n"
        f"{capabilities_text}\n\n"
        f"## Примеры запросов\n"
        f"{examples_text}\n\n"
        f"## Инструкции\n"
        f"1. Определи что именно нужно пользователю\n"
        f"2. Выполни задачу используя доступные инструменты\n"
        f"3. Предоставь результат в структурированном формате\n"
    )

    # Валидация
    ok, errors = validate_skill(skill_name, content, agent_dir)
    if not ok:
        return False, f"Валидация: {', '.join(errors)}"

    # Установка
    install_skill(skill_name, content, agent_dir)
    return True, f"Скилл '{skill_name}' создан из предложения SkillAdvisor"


def get_all_agent_dirs(project_root: str) -> dict[str, str]:
    """
    Найти все директории агентов.

    Returns:
        {agent_name: agent_dir_path}
    """
    agents_dir = Path(project_root) / "agents"
    if not agents_dir.exists():
        return {}

    result = {}
    for agent_yaml in agents_dir.glob("*/agent.yaml"):
        try:
            with open(agent_yaml, encoding="utf-8") as f:
                config = yaml.safe_load(f.read())
            name = config.get("name", agent_yaml.parent.name)
            result[name] = str(agent_yaml.parent)
        except Exception:
            continue
    return result
