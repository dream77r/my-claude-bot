"""Тесты для file_handler.py."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.file_handler import MAX_FILE_SIZE, download_file, send_file
from src.memory import ensure_dirs


@pytest.fixture
def agent_dir(tmp_path):
    """Создать временную директорию агента."""
    agent = tmp_path / "agents" / "test"
    agent.mkdir(parents=True)
    ensure_dirs(str(agent))
    return str(agent)


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_download_success(self, agent_dir):
        # Мок Telegram Bot и File
        mock_file = AsyncMock()
        mock_file.file_size = 1024
        mock_file.file_path = "documents/test.pdf"
        mock_file.download_to_drive = AsyncMock()

        mock_bot = AsyncMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        path = await download_file(mock_bot, "file-123", agent_dir)

        assert "test.pdf" in path
        mock_bot.get_file.assert_called_once_with("file-123")
        mock_file.download_to_drive.assert_called_once()

    @pytest.mark.asyncio
    async def test_file_too_large(self, agent_dir):
        mock_file = AsyncMock()
        mock_file.file_size = MAX_FILE_SIZE + 1
        mock_file.file_path = "documents/huge.zip"

        mock_bot = AsyncMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        with pytest.raises(ValueError, match="слишком большой"):
            await download_file(mock_bot, "file-123", agent_dir)

    @pytest.mark.asyncio
    async def test_download_creates_dir(self, tmp_path):
        agent = tmp_path / "agents" / "newagent"
        agent.mkdir(parents=True)
        # Не вызываем ensure_dirs — download должен сам создать

        mock_file = AsyncMock()
        mock_file.file_size = 100
        mock_file.file_path = "documents/small.txt"
        mock_file.download_to_drive = AsyncMock()

        mock_bot = AsyncMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        path = await download_file(mock_bot, "file-123", str(agent))
        assert "small.txt" in path

    @pytest.mark.asyncio
    async def test_get_file_error(self, agent_dir):
        mock_bot = AsyncMock()
        mock_bot.get_file = AsyncMock(side_effect=Exception("Telegram error"))

        with pytest.raises(RuntimeError, match="получить файл"):
            await download_file(mock_bot, "file-123", agent_dir)


class TestSendFile:
    @pytest.mark.asyncio
    async def test_send_success(self, tmp_path):
        test_file = tmp_path / "output.txt"
        test_file.write_text("Hello!")

        mock_bot = AsyncMock()
        mock_bot.send_document = AsyncMock()

        await send_file(mock_bot, 12345, str(test_file))
        mock_bot.send_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        mock_bot = AsyncMock()
        with pytest.raises(FileNotFoundError):
            await send_file(mock_bot, 12345, "/nonexistent/file.txt")

    @pytest.mark.asyncio
    async def test_file_too_large_for_send(self, tmp_path):
        test_file = tmp_path / "huge.bin"
        # Создаём файл > 50MB (виртуально через запись заголовка)
        with open(test_file, "wb") as f:
            f.seek(51 * 1024 * 1024)
            f.write(b"\0")

        mock_bot = AsyncMock()
        with pytest.raises(ValueError, match="слишком большой"):
            await send_file(mock_bot, 12345, str(test_file))
