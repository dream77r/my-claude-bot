"""Тесты для voice_handler.py."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.memory import ensure_dirs
from src.voice_handler import download_voice, get_deepgram_api_key, transcribe


@pytest.fixture
def agent_dir(tmp_path):
    """Создать временную директорию агента."""
    agent = tmp_path / "agents" / "test"
    agent.mkdir(parents=True)
    ensure_dirs(str(agent))
    return str(agent)


@pytest.fixture
def ogg_file(tmp_path):
    """Создать фейковый OGG файл."""
    audio = tmp_path / "test_voice.ogg"
    # Минимальный OGG заголовок (не валидный, но достаточный для тестов)
    audio.write_bytes(b"OggS" + b"\x00" * 100)
    return str(audio)


class TestGetDeepgramApiKey:
    def test_from_settings(self, agent_dir):
        import json
        settings_path = Path(agent_dir) / "memory" / "settings.json"
        settings_path.write_text(
            json.dumps({"deepgram_api_key": "from-settings"}),
            encoding="utf-8",
        )
        assert get_deepgram_api_key(agent_dir) == "from-settings"

    def test_from_env(self, agent_dir):
        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "from-env"}):
            assert get_deepgram_api_key(agent_dir) == "from-env"

    def test_settings_takes_priority_over_env(self, agent_dir):
        import json
        settings_path = Path(agent_dir) / "memory" / "settings.json"
        settings_path.write_text(
            json.dumps({"deepgram_api_key": "from-settings"}),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "from-env"}):
            assert get_deepgram_api_key(agent_dir) == "from-settings"

    def test_returns_none_if_nothing_configured(self, agent_dir):
        with patch.dict("os.environ", {}, clear=True):
            assert get_deepgram_api_key(agent_dir) is None

    def test_no_agent_dir_uses_env(self):
        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "from-env"}):
            assert get_deepgram_api_key() == "from-env"


class TestDownloadVoice:
    @pytest.mark.asyncio
    async def test_download_success(self, agent_dir):
        mock_file = AsyncMock()
        mock_file.file_size = 5000
        mock_file.download_to_drive = AsyncMock()

        mock_bot = AsyncMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        path = await download_voice(mock_bot, "voice-123", agent_dir)

        assert path.endswith(".ogg")
        assert "voice" in Path(path).parent.name
        mock_bot.get_file.assert_called_once_with("voice-123")
        mock_file.download_to_drive.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_voice_dir(self, tmp_path):
        agent = tmp_path / "agents" / "newagent"
        agent.mkdir(parents=True)

        mock_file = AsyncMock()
        mock_file.file_size = 100
        mock_file.download_to_drive = AsyncMock()

        mock_bot = AsyncMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        path = await download_voice(mock_bot, "voice-123", str(agent))
        assert Path(path).parent.exists()


class TestTranscribe:
    @pytest.mark.asyncio
    async def test_transcribe_success(self, ogg_file):
        deepgram_response = {
            "results": {
                "channels": [
                    {
                        "alternatives": [
                            {
                                "transcript": "Привет, это тестовое сообщение",
                                "confidence": 0.98,
                            }
                        ]
                    }
                ]
            }
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = deepgram_response

        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "test-key"}):
            with patch("src.voice_handler.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await transcribe(ogg_file)

        assert result == "Привет, это тестовое сообщение"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "Token test-key" in call_kwargs.kwargs["headers"]["Authorization"]
        assert call_kwargs.kwargs["params"]["model"] == "nova-3"
        assert call_kwargs.kwargs["params"]["language"] == "ru"

    @pytest.mark.asyncio
    async def test_transcribe_custom_language(self, ogg_file):
        deepgram_response = {
            "results": {
                "channels": [
                    {"alternatives": [{"transcript": "Hello world", "confidence": 0.99}]}
                ]
            }
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = deepgram_response

        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "test-key"}):
            with patch("src.voice_handler.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await transcribe(ogg_file, language="en")

        assert result == "Hello world"
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["params"]["language"] == "en"

    @pytest.mark.asyncio
    async def test_no_api_key(self, ogg_file):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
                await transcribe(ogg_file)

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "test-key"}):
            with pytest.raises(FileNotFoundError):
                await transcribe("/nonexistent/audio.ogg")

    @pytest.mark.asyncio
    async def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.ogg"
        empty.write_bytes(b"")

        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "test-key"}):
            with pytest.raises(ValueError, match="пустой"):
                await transcribe(str(empty))

    @pytest.mark.asyncio
    async def test_api_error(self, ogg_file):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "bad-key"}):
            with patch("src.voice_handler.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                with pytest.raises(RuntimeError, match="HTTP 401"):
                    await transcribe(ogg_file)

    @pytest.mark.asyncio
    async def test_empty_transcript(self, ogg_file):
        deepgram_response = {
            "results": {
                "channels": [
                    {"alternatives": [{"transcript": "   ", "confidence": 0.1}]}
                ]
            }
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = deepgram_response

        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "test-key"}):
            with patch("src.voice_handler.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await transcribe(ogg_file)

        assert "не распознана" in result

    @pytest.mark.asyncio
    async def test_malformed_response(self, ogg_file):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": {}}

        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "test-key"}):
            with patch("src.voice_handler.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                with pytest.raises(RuntimeError, match="извлечь текст"):
                    await transcribe(ogg_file)
