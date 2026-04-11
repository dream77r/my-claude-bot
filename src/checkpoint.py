"""
Checkpoint Recovery — сохранение и восстановление состояния при крэше.

При крэше mid-tool-call (OOM, сетевая ошибка, рестарт сервера) —
восстанавливает контекст и сообщает пользователю что произошло.

Хранение: memory/sessions/checkpoint.json
Формат: {prompt, session_id, started_at, tools_used, partial_text}

Жизненный цикл:
1. before_call → создаёт checkpoint
2. on_tool_use → обновляет tools_used
3. after_call → удаляет checkpoint (вызов завершён успешно)
4. on_error → помечает checkpoint как error
5. При старте → проверяет наличие checkpoint (= прерванный вызов)
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from . import memory

logger = logging.getLogger(__name__)

CHECKPOINT_FILE = "sessions/checkpoint.json"


def _checkpoint_path(agent_dir: str) -> Path:
    """Путь к файлу checkpoint."""
    return memory.get_memory_path(agent_dir) / CHECKPOINT_FILE


def save(
    agent_dir: str,
    prompt: str,
    session_id: str | None = None,
) -> None:
    """Создать checkpoint перед вызовом Claude."""
    path = _checkpoint_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "prompt": prompt[:500],  # Не хранить полный промпт (экономия)
        "session_id": session_id,
        "started_at": datetime.now().isoformat(),
        "tools_used": [],
        "partial_text": "",
        "status": "in_progress",
    }

    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Checkpoint save error: {e}")


def update_tool(agent_dir: str, tool_name: str) -> None:
    """Добавить tool call в checkpoint."""
    path = _checkpoint_path(agent_dir)
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tools = data.get("tools_used", [])
        tools.append({
            "tool": tool_name,
            "ts": datetime.now().isoformat(),
        })
        # Хранить максимум 20 последних tool calls
        data["tools_used"] = tools[-20:]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Checkpoint update error: {e}")


def update_text(agent_dir: str, partial_text: str) -> None:
    """Обновить partial_text в checkpoint (последний текстовый фрагмент)."""
    path = _checkpoint_path(agent_dir)
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["partial_text"] = partial_text[-500:]  # Только хвост
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Checkpoint text update error: {e}")


def mark_error(agent_dir: str, error: str) -> None:
    """Пометить checkpoint как завершившийся ошибкой."""
    path = _checkpoint_path(agent_dir)
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "error"
        data["error"] = str(error)[:200]
        data["ended_at"] = datetime.now().isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Checkpoint mark_error: {e}")


def clear(agent_dir: str) -> None:
    """Удалить checkpoint (вызов завершён успешно)."""
    path = _checkpoint_path(agent_dir)
    if path.exists():
        try:
            path.unlink()
        except Exception as e:
            logger.error(f"Checkpoint clear error: {e}")


def recover(agent_dir: str) -> dict | None:
    """
    Проверить наличие прерванного checkpoint.

    Вызывать при старте агента.

    Returns:
        dict с данными checkpoint или None если нет прерванного вызова.
    """
    path = _checkpoint_path(agent_dir)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Checkpoint corrupt, removing: {e}")
        path.unlink(missing_ok=True)
        return None

    status = data.get("status", "unknown")

    # Если статус in_progress — это прерванный вызов (крэш)
    if status == "in_progress":
        logger.warning(
            f"Обнаружен прерванный вызов: prompt={data.get('prompt', '')[:50]}... "
            f"tools={len(data.get('tools_used', []))}"
        )
        return data

    # Если error — уже обработано, но не удалено
    if status == "error":
        logger.info("Обнаружен checkpoint с ошибкой, очищаю")
        path.unlink(missing_ok=True)
        return None

    # Неизвестный статус — удалить
    path.unlink(missing_ok=True)
    return None


def format_recovery_message(checkpoint_data: dict) -> str:
    """Сформировать сообщение о восстановлении для пользователя."""
    prompt = checkpoint_data.get("prompt", "")
    started = checkpoint_data.get("started_at", "")
    tools = checkpoint_data.get("tools_used", [])
    partial = checkpoint_data.get("partial_text", "")

    lines = ["Обнаружен прерванный запрос:"]

    if prompt:
        lines.append(f"Запрос: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")

    if started:
        lines.append(f"Начат: {started}")

    if tools:
        tool_names = [t.get("tool", "?") for t in tools[-5:]]
        lines.append(f"Инструменты: {', '.join(tool_names)}")

    if partial:
        lines.append(f"Частичный ответ: {partial[:100]}...")

    lines.append("")
    lines.append("Сессия была сброшена. Можешь повторить запрос.")

    return "\n".join(lines)


def make_checkpoint_hooks(agent_dir: str):
    """
    Создать хуки для автоматического checkpoint.

    Returns:
        (before_fn, tool_fn, after_fn, error_fn)
    """
    from .hooks import HookContext

    async def _before(ctx: HookContext) -> HookContext:
        session_id = None  # Session ID доступен в agent, но не в хуке
        save(agent_dir, ctx.data.get("message", ""), session_id)
        return ctx

    async def _tool(ctx: HookContext) -> HookContext:
        tool_name = ctx.data.get("tool_name", "")
        update_tool(agent_dir, tool_name)
        return ctx

    async def _after(ctx: HookContext) -> HookContext:
        clear(agent_dir)
        return ctx

    async def _error(ctx: HookContext) -> HookContext:
        error = ctx.data.get("error", "")
        mark_error(agent_dir, str(error))
        return ctx

    return _before, _tool, _after, _error
