"""
Consolidator — умное сжатие контекста при длинных разговорах.

Когда разговор приближается к лимиту контекстного окна:
1. Суммаризирует историю через дешёвую модель (haiku)
2. Сохраняет сводку в memory/sessions/conversation_summary.md
3. Очищает сессию Claude CLI (новый --resume)
4. Следующий вызов начинает свежую сессию со сводкой в system prompt

Конфиг в agent.yaml:
  consolidator:
    enabled: true
    max_turns: 80           # макс. пар (вопрос-ответ) до сжатия
    max_chars: 400000       # макс. символов (~120K токенов, ~60% контекста)
    max_input_tokens: 160000  # token-budget: реальный размер контекста по
                              # ResultMessage.usage (input + cache_read +
                              # cache_creation). 160K = 80% от 200K окна
                              # Sonnet. Для Opus 1M можно 800000.
    summary_model: haiku    # модель для суммаризации
"""

import logging
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from . import memory
from . import get_claude_cli_path

logger = logging.getLogger(__name__)

# Промпт для суммаризации
_SUMMARIZE_PROMPT = """\
Ниже — история разговора между пользователем и AI-ассистентом.
Создай краткую сводку для продолжения диалога в новой сессии.

Включи:
1. **Основные темы** — о чём говорили (2-5 пунктов)
2. **Ключевые решения** — что было решено или согласовано
3. **Незавершённые задачи** — что осталось сделать
4. **Важный контекст** — факты, предпочтения, ограничения

Формат: Markdown, кратко (до 1500 символов). Пиши на том же языке,
что и разговор.

---

{conversation}
"""


class Consolidator:
    """
    Отслеживает объём разговора и автоматически сжимает контекст.

    Работает с Claude CLI session management:
    - Считает turns (пары вопрос-ответ) и символы
    - При достижении лимита — суммаризирует через haiku
    - Очищает session_id → новая сессия со сводкой в system prompt
    """

    def __init__(self, agent_dir: str, config: dict):
        self.agent_dir = agent_dir
        self.max_turns = config.get("max_turns", 80)
        self.max_chars = config.get("max_chars", 400_000)
        # Token-budget: триггерит compaction, когда реальный input_tokens
        # из последнего ResultMessage приближается к окну модели. Это
        # точнее чем max_chars (наша оценка) — берём цифру от Anthropic.
        # 0 = выключено.
        self.max_input_tokens = int(config.get("max_input_tokens", 160_000))
        self.summary_model = config.get("summary_model", "haiku")

        # Трекинг текущей сессии
        self._turns = 0
        self._total_chars = 0
        self._history: list[tuple[str, str]] = []  # (role, content)
        # Полный размер контекста (input + cache_read + cache_creation)
        # из последнего ResultMessage.usage. Обновляется в Agent._process_message.
        self._last_input_tokens: int = 0

        # Загрузить сохранённую сводку (если есть)
        self._summary = self._load_summary()

    def track(self, user_message: str, assistant_response: str) -> None:
        """Отследить пару сообщений."""
        self._turns += 1
        self._total_chars += len(user_message) + len(assistant_response)
        self._history.append(("user", user_message))
        self._history.append(("assistant", assistant_response))

    def update_token_usage(self, usage: dict | None) -> None:
        """
        Обновить трекинг реального размера контекста из ResultMessage.usage.

        Полный размер = input_tokens + cache_read_input_tokens +
        cache_creation_input_tokens. Это то, сколько токенов Claude
        реально видит в контексте при следующем --resume.
        """
        if not usage:
            return
        try:
            total = (
                int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("cache_read_input_tokens", 0) or 0)
                + int(usage.get("cache_creation_input_tokens", 0) or 0)
            )
        except (TypeError, ValueError):
            return
        if total > 0:
            self._last_input_tokens = total

    def needs_consolidation(self) -> bool:
        """Проверить, нужно ли сжатие.

        Три независимых триггера (любой → True):
        - max_turns: история диалога стала длинной
        - max_chars: оценка размера превысила лимит
        - max_input_tokens: реальный input_tokens по usage близок к окну
        """
        if self._turns >= self.max_turns:
            return True
        if self._total_chars >= self.max_chars:
            return True
        if (
            self.max_input_tokens > 0
            and self._last_input_tokens >= self.max_input_tokens
        ):
            return True
        return False

    async def consolidate(self) -> str | None:
        """
        Суммаризировать разговор и начать новую сессию.

        Returns:
            Текст сводки или None если сжатие не нужно.
        """
        if not self.needs_consolidation():
            return None

        if not self._history:
            return None

        logger.info(
            f"Consolidator: сжатие ({self._turns} turns, "
            f"{self._total_chars} chars)"
        )

        # Собрать текст для суммаризации
        conversation_text = self._format_history()

        # Суммаризировать
        summary = await self._summarize(conversation_text)
        if not summary:
            # Fallback: просто сбросить без сводки
            logger.warning("Consolidator: суммаризация не удалась, сброс без сводки")
            self._reset()
            return None

        # Сохранить сводку
        self._summary = summary
        self._save_summary(summary)

        # Очистить сессию — следующий вызов создаст новую
        memory.clear_session_id(self.agent_dir)

        # Сбросить трекинг
        self._reset()

        logger.info(f"Consolidator: сжатие завершено, сводка {len(summary)} символов")
        return summary

    def get_summary(self) -> str | None:
        """Получить сохранённую сводку для system prompt."""
        return self._summary

    def clear_summary(self) -> None:
        """Очистить сводку (при /newsession)."""
        self._summary = None
        summary_path = self._summary_path()
        if summary_path.exists():
            summary_path.unlink()
        self._reset()

    def _reset(self) -> None:
        """Сбросить трекинг."""
        self._turns = 0
        self._total_chars = 0
        self._history.clear()
        # После consolidate session_id чистится, следующий turn стартует
        # с минимальным контекстом — обнуляем и token tracking, иначе
        # триггер останется hot и compaction зациклится.
        self._last_input_tokens = 0

    def _format_history(self) -> str:
        """Отформатировать историю для суммаризации."""
        parts = []
        for role, content in self._history:
            prefix = "👤 User" if role == "user" else "🤖 Assistant"
            # Обрезать длинные сообщения
            text = content if len(content) <= 500 else content[:497] + "..."
            parts.append(f"**{prefix}:** {text}")
        return "\n\n".join(parts)

    async def _summarize(self, conversation_text: str) -> str | None:
        """Вызвать дешёвую модель для суммаризации."""
        prompt = _SUMMARIZE_PROMPT.format(conversation=conversation_text)

        try:
            options = ClaudeAgentOptions(
                system_prompt="Ты — помощник для суммаризации разговоров.",
                permission_mode="bypassPermissions",
                model=self.summary_model,
                cli_path=get_claude_cli_path(),
            )

            result_text = ""
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, ResultMessage) and msg.result:
                    result_text = msg.result

            return result_text if result_text else None

        except Exception as e:
            logger.error(f"Consolidator summarize error: {e}")
            return None

    def _summary_path(self) -> Path:
        """Путь к файлу сводки."""
        return memory.get_memory_path(self.agent_dir) / "sessions" / "conversation_summary.md"

    def _save_summary(self, summary: str) -> None:
        """Сохранить сводку в файл."""
        path = self._summary_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(summary, encoding="utf-8")

    def _load_summary(self) -> str | None:
        """Загрузить сохранённую сводку."""
        path = self._summary_path()
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            return text if text else None
        return None
