"""Тесты retry-обёртки для Telegram API."""

from unittest.mock import patch

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from src.telegram_retry import tg_retry


@pytest.mark.asyncio
async def test_success_first_try():
    calls = 0

    async def ok():
        nonlocal calls
        calls += 1
        return "hi"

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        result = await tg_retry(lambda: ok())
    assert result == "hi"
    assert calls == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_retries_on_network_error_then_succeeds():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise NetworkError("boom")
        return "ok"

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        result = await tg_retry(lambda: flaky(), base_delay=1.0)
    assert result == "ok"
    assert calls == 3
    # Две задержки: 1s, 2s
    assert sleep.call_count == 2
    assert sleep.call_args_list[0].args[0] == 1.0
    assert sleep.call_args_list[1].args[0] == 2.0


@pytest.mark.asyncio
async def test_retries_on_timed_out():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise TimedOut("slow")
        return 42

    with patch("src.telegram_retry.asyncio.sleep"):
        assert await tg_retry(lambda: flaky()) == 42
    assert calls == 2


@pytest.mark.asyncio
async def test_exponential_backoff_sequence():
    async def always_fail():
        raise NetworkError("nope")

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        with pytest.raises(NetworkError):
            await tg_retry(lambda: always_fail(), attempts=4, base_delay=1.0)
    # 4 попытки = 3 задержки: 1, 2, 4
    assert [c.args[0] for c in sleep.call_args_list] == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_retry_after_respects_server_delay():
    calls = 0

    async def flooded():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryAfter(3)
        return "done"

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        result = await tg_retry(lambda: flooded())
    assert result == "done"
    assert calls == 2
    # RetryAfter(3) + 0.5 страховка
    assert sleep.call_args_list[0].args[0] == 3.5


@pytest.mark.asyncio
async def test_retry_after_caps_absurd_delays():
    async def flooded():
        raise RetryAfter(9999)  # сервер шутит

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        with pytest.raises(RetryAfter):
            await tg_retry(lambda: flooded(), attempts=2)
    # Должен был зажаться к MAX_RETRY_AFTER=60
    assert sleep.call_args_list[0].args[0] == 60.0


@pytest.mark.asyncio
async def test_bad_request_not_retried():
    calls = 0

    async def bad():
        nonlocal calls
        calls += 1
        raise BadRequest("chat not found")

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        with pytest.raises(BadRequest):
            await tg_retry(lambda: bad())
    assert calls == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_forbidden_not_retried():
    calls = 0

    async def forbidden():
        nonlocal calls
        calls += 1
        raise Forbidden("bot was blocked")

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        with pytest.raises(Forbidden):
            await tg_retry(lambda: forbidden())
    assert calls == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_non_telegram_error_not_retried():
    async def weird():
        raise ValueError("not telegram")

    with patch("src.telegram_retry.asyncio.sleep") as sleep:
        with pytest.raises(ValueError):
            await tg_retry(lambda: weird())
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_factory_called_fresh_each_attempt():
    """Корутина не переиспользуется — каждая попытка создаёт новую."""
    made = 0
    ran = 0

    def factory():
        nonlocal made
        made += 1

        async def coro():
            nonlocal ran
            ran += 1
            if ran < 2:
                raise NetworkError("x")
            return "ok"

        return coro()

    with patch("src.telegram_retry.asyncio.sleep"):
        assert await tg_retry(factory) == "ok"
    assert made == 2  # фабрика вызвана на каждой попытке
    assert ran == 2
