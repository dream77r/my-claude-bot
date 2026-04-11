"""
Обработка голосовых сообщений: скачивание OGG из Telegram + транскрипция через Deepgram API.

Deepgram Nova-3 — быстрая и точная модель для русского языка.
Стоимость: ~$0.0043/мин (pay-as-you-go).

Используем httpx напрямую (уже зависимость python-telegram-bot) вместо deepgram-sdk
для простоты и минимума зависимостей.
"""

import logging
import os
from pathlib import Path

import httpx
from telegram import Bot

from . import memory

logger = logging.getLogger(__name__)

DEEPGRAM_API_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_TIMEOUT = 30  # секунд


def get_deepgram_api_key(
    agent_dir: str | None = None,
    master_agent_dir: str | None = None,
) -> str | None:
    """
    Получить API ключ Deepgram.

    Каскадный поиск:
    1. settings.json агента (свой ключ, введён через чат)
    2. settings.json master-агента (общий ключ)
    3. Переменная окружения DEEPGRAM_API_KEY (из .env)
    """
    # 1. Свой ключ агента
    if agent_dir:
        key = memory.get_setting(agent_dir, "deepgram_api_key")
        if key:
            return key

    # 2. Ключ master-агента (общий для всего fleet)
    if master_agent_dir and master_agent_dir != agent_dir:
        key = memory.get_setting(master_agent_dir, "deepgram_api_key")
        if key:
            return key

    # 3. Переменная окружения
    return os.environ.get("DEEPGRAM_API_KEY")


async def download_voice(bot: Bot, file_id: str, agent_dir: str) -> str:
    """
    Скачать голосовое сообщение из Telegram.

    Args:
        bot: Telegram Bot instance
        file_id: ID файла в Telegram
        agent_dir: путь к директории агента

    Returns:
        Локальный путь к скачанному OGG файлу
    """
    from datetime import datetime

    voice_dir = Path(agent_dir) / "memory" / "raw" / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)

    tg_file = await bot.get_file(file_id)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_path = voice_dir / f"voice_{timestamp}.ogg"

    await tg_file.download_to_drive(str(local_path))
    logger.info(f"Голосовое скачано: {local_path} ({tg_file.file_size or '?'} bytes)")
    return str(local_path)


async def transcribe(
    audio_path: str,
    language: str = "ru",
    agent_dir: str | None = None,
    master_agent_dir: str | None = None,
) -> str:
    """
    Транскрибировать аудиофайл через Deepgram API.

    Args:
        audio_path: путь к OGG файлу
        language: код языка (по умолчанию "ru")
        agent_dir: путь к директории агента (для чтения ключа из settings.json)
        master_agent_dir: путь к master-агенту (fallback для общего ключа)

    Returns:
        Текст транскрипции

    Raises:
        RuntimeError: если транскрипция не удалась
        ValueError: если нет API ключа
    """
    api_key = get_deepgram_api_key(agent_dir, master_agent_dir)
    if not api_key:
        raise ValueError(
            "DEEPGRAM_API_KEY не задан. Добавь его в .env файл."
        )

    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")

    audio_data = path.read_bytes()
    if not audio_data:
        raise ValueError("Аудиофайл пустой")

    params = {
        "model": "nova-3",
        "language": language,
        "smart_format": "true",
    }

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/ogg",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            DEEPGRAM_API_URL,
            params=params,
            headers=headers,
            content=audio_data,
            timeout=DEEPGRAM_TIMEOUT,
        )

    if response.status_code != 200:
        logger.error(f"Deepgram API error {response.status_code}: {response.text}")
        raise RuntimeError(
            f"Ошибка транскрипции (HTTP {response.status_code})"
        )

    data = response.json()

    try:
        transcript = (
            data["results"]["channels"][0]["alternatives"][0]["transcript"]
        )
    except (KeyError, IndexError):
        logger.error(f"Unexpected Deepgram response: {data}")
        raise RuntimeError("Не удалось извлечь текст из ответа Deepgram")

    if not transcript.strip():
        return "(пустое голосовое сообщение — речь не распознана)"

    logger.info(f"Транскрипция: {len(transcript)} символов")
    return transcript
