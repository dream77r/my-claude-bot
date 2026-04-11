"""
Tool Hints — человеко-читаемые статусы инструментов для Telegram.

Когда агент выполняет инструменты (Read, Write, WebSearch и т.д.),
пользователь видит в чате реальный статус вместо просто "typing...".
"""

import os


# Маппинг инструментов → шаблоны на русском
_TOOL_FORMATS: dict[str, dict] = {
    "Read": {
        "keys": ["file_path"],
        "template": "Читаю {}",
        "is_path": True,
    },
    "Write": {
        "keys": ["file_path"],
        "template": "Записываю {}",
        "is_path": True,
    },
    "Edit": {
        "keys": ["file_path"],
        "template": "Редактирую {}",
        "is_path": True,
    },
    "Glob": {
        "keys": ["pattern"],
        "template": "Ищу файлы: {}",
    },
    "Grep": {
        "keys": ["pattern"],
        "template": "Ищу в коде: {}",
    },
    "WebSearch": {
        "keys": ["query"],
        "template": "Ищу в интернете: {}",
    },
    "WebFetch": {
        "keys": ["url"],
        "template": "Загружаю страницу: {}",
    },
    "Bash": {
        "keys": ["command"],
        "template": "Выполняю: {}",
    },
}


def format_tool_hint(tool_name: str, tool_input: dict) -> str:
    """
    Превратить tool_use событие в читаемый статус.

    Args:
        tool_name: имя инструмента (Read, Write, Grep, ...)
        tool_input: словарь параметров инструмента

    Returns:
        Строка вида "Читаю config.py..." или "Ищу в интернете: query..."
    """
    fmt = _TOOL_FORMATS.get(tool_name)
    if not fmt:
        return f"Использую {tool_name}..."

    # Найти первый подходящий ключ
    value = None
    for key in fmt["keys"]:
        if key in tool_input:
            value = str(tool_input[key])
            break

    if not value:
        return f"Использую {tool_name}..."

    # Для путей — показать только имя файла
    if fmt.get("is_path"):
        value = os.path.basename(value)

    # Для команд — показать первые 40 символов
    if tool_name == "Bash" and len(value) > 40:
        value = value[:37] + "..."

    # Общее ограничение длины
    if len(value) > 60:
        value = value[:57] + "..."

    return fmt["template"].format(value) + "..."
