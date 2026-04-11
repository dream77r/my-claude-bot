"""
Работа с файлами: скачивание из Telegram, отправка обратно.

Файлы сохраняются в agents/{name}/memory/raw/files/
с уникальными именами (timestamp + оригинальное имя).

Outbox: agents/{name}/memory/outbox/ — файлы для отправки пользователю.
Claude кладёт туда файлы, pipeline сканирует и отправляет в Telegram.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from telegram import Bot

logger = logging.getLogger(__name__)

# Директория outbox (относительно agent_dir)
OUTBOX_SUBDIR = "memory/outbox"

# Максимальный размер файла для скачивания (20MB)
MAX_FILE_SIZE = 20 * 1024 * 1024


async def download_file(bot: Bot, file_id: str, agent_dir: str) -> str:
    """
    Скачать файл из Telegram в raw/files/.

    Args:
        bot: Telegram Bot instance
        file_id: ID файла в Telegram
        agent_dir: путь к директории агента

    Returns:
        Локальный путь к скачанному файлу

    Raises:
        ValueError: если файл слишком большой
        RuntimeError: если не удалось скачать
    """
    files_dir = Path(agent_dir) / "memory" / "raw" / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    try:
        tg_file = await bot.get_file(file_id)
    except Exception as e:
        raise RuntimeError(f"Не удалось получить файл: {e}") from e

    # Проверка размера
    if tg_file.file_size and tg_file.file_size > MAX_FILE_SIZE:
        raise ValueError(
            f"Файл слишком большой: {tg_file.file_size / 1024 / 1024:.1f}MB "
            f"(макс. {MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
        )

    # Сформировать имя файла: timestamp_originalname
    original_name = Path(tg_file.file_path).name if tg_file.file_path else "file"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_name = f"{timestamp}_{original_name}"
    local_path = files_dir / local_name

    try:
        await tg_file.download_to_drive(str(local_path))
    except Exception as e:
        raise RuntimeError(f"Не удалось скачать файл: {e}") from e

    logger.info(f"Файл скачан: {local_path} ({tg_file.file_size or '?'} bytes)")
    return str(local_path)


async def send_file(bot: Bot, chat_id: int, file_path: str) -> None:
    """
    Отправить файл в Telegram чат.

    Args:
        bot: Telegram Bot instance
        chat_id: ID чата
        file_path: путь к локальному файлу

    Raises:
        FileNotFoundError: если файл не найден
        RuntimeError: если не удалось отправить
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    file_size = path.stat().st_size
    if file_size > 50 * 1024 * 1024:  # Telegram лимит 50MB для ботов
        raise ValueError(f"Файл слишком большой для отправки: {file_size / 1024 / 1024:.1f}MB")

    try:
        with open(path, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=path.name,
            )
        logger.info(f"Файл отправлен: {path.name} → chat {chat_id}")
    except Exception as e:
        raise RuntimeError(f"Не удалось отправить файл: {e}") from e


def scan_outbox(agent_dir: str) -> list[str]:
    """
    Сканировать outbox на наличие файлов для отправки.

    Returns:
        Список абсолютных путей к файлам в outbox
    """
    outbox = Path(agent_dir) / OUTBOX_SUBDIR
    if not outbox.exists():
        return []

    files = []
    for item in sorted(outbox.iterdir()):
        if item.is_file() and not item.name.startswith("."):
            files.append(str(item))

    return files


def clear_outbox(agent_dir: str) -> int:
    """
    Архивировать файлы из outbox в raw/files/sent/, затем удалить из outbox.

    Returns:
        Количество обработанных файлов
    """
    outbox = Path(agent_dir) / OUTBOX_SUBDIR
    if not outbox.exists():
        return 0

    sent_dir = Path(agent_dir) / "memory" / "raw" / "files" / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    count = 0
    for item in outbox.iterdir():
        if item.is_file():
            try:
                archive_name = f"{timestamp}_{item.name}"
                shutil.copy2(item, sent_dir / archive_name)
                item.unlink()
                count += 1
            except OSError as e:
                logger.warning(f"Не удалось обработать {item}: {e}")

    return count
