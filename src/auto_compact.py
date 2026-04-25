"""
AutoCompact — фоновое сжатие контекста в простое.

Дополняет post-turn consolidation в Agent.call_claude: тот компактится
ТОЛЬКО на конце успешного turn. Если юзер замолчал, накопив тяжёлый
контекст, давление на следующий turn остаётся. Этот loop проходится
по всем агентам периодически и зовёт consolidator.consolidate(), если:

  1. У агента есть consolidator (включён в agent.yaml).
  2. consolidator.needs_consolidation() == True.
  3. Worker агента не is_busy() — иначе можно затереть сессию из-под
     активного turn'а (consolidate чистит session_id).

Идея взята из nanobot HKUDS — двухуровневый AutoCompact (token-budget
из call_claude + idle-trigger отсюда). Snapshot из reference_nanobot.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import FleetRuntime

logger = logging.getLogger(__name__)

# Период между проверками. 5 минут — компромисс: достаточно часто, чтобы
# поймать idle между активными бёрстами, но не нагружает haiku-вызовами.
DEFAULT_INTERVAL_SECONDS = 300


async def auto_compact_loop(
    runtime: "FleetRuntime",
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Бесконечный loop, периодически жмёт idle-агентов."""
    logger.info(
        f"AutoCompact loop запущен (интервал {interval_seconds}с)"
    )
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await _tick(runtime)
        except asyncio.CancelledError:
            logger.info("AutoCompact loop остановлен")
            break
        except Exception as e:
            # Loop не должен падать из-за ошибки в одном агенте.
            logger.error(f"AutoCompact loop tick error: {e}")


async def _tick(runtime: "FleetRuntime") -> None:
    """Один проход по всем агентам."""
    # Снимок имён, чтобы не падать на изменении словаря во время итерации
    # (hot-reload может стартовать/останавливать агентов в любой момент).
    names = list(runtime.agents.keys())

    for name in names:
        agent = runtime.agents.get(name)
        if agent is None or agent.consolidator is None:
            continue

        try:
            if not agent.consolidator.needs_consolidation():
                continue
        except Exception as e:
            logger.warning(f"AutoCompact: needs_consolidation '{name}': {e}")
            continue

        worker = runtime.workers.get(name)
        if worker is not None and worker.is_busy():
            # Активный turn — не трогаем сессию. Попробуем в следующий тик.
            logger.debug(
                f"AutoCompact: '{name}' busy, пропускаю compaction"
            )
            continue

        logger.info(f"AutoCompact: idle compaction для '{name}'")
        try:
            await agent.consolidator.consolidate()
        except Exception as e:
            logger.error(f"AutoCompact: consolidate '{name}' error: {e}")
