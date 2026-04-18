"""Tests for src/skill_advisor — hot-path coverage.

skill_advisor.py до сих пор был без тестов, и в нём были регрессии.
Покрываем чистые функции (collect/format/store/compile) + receiver
через реальный bus.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src import skill_advisor
from src.bus import FleetBus, FleetMessage, MessageType


def _agent_dir(tmp_path: Path, name: str = "me") -> Path:
    p = tmp_path / "agents" / name
    (p / "memory").mkdir(parents=True, exist_ok=True)
    return p


def _write_conv(
    agent_dir: Path, date: str, messages: list[dict]
) -> Path:
    conv_dir = agent_dir / "memory" / "raw" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / f"conversations-{date}.jsonl"
    path.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in messages),
        encoding="utf-8",
    )
    return path


class TestCollectConversations:
    def test_returns_empty_when_no_conv_dir(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        assert skill_advisor._collect_conversations(str(agent_dir)) == []

    def test_collects_recent_messages(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        _write_conv(
            agent_dir, today,
            [
                {"timestamp": "2026-04-18T10:00", "role": "user", "content": "hi"},
                {"timestamp": "2026-04-18T10:01", "role": "assistant", "content": "hello"},
            ],
        )
        msgs = skill_advisor._collect_conversations(str(agent_dir), days=7)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "hi"

    def test_filters_out_old_files(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        _write_conv(
            agent_dir, old,
            [{"timestamp": "x", "role": "user", "content": "ancient"}],
        )
        assert skill_advisor._collect_conversations(str(agent_dir), days=7) == []

    def test_skips_invalid_json_lines(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        conv_dir = agent_dir / "memory" / "raw" / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / f"conversations-{today}.jsonl").write_text(
            "not-json\n{}\n", encoding="utf-8"
        )
        # Не падает, но ничего не возвращает (две строки: "not-json"
        # невалидный — пропускается целиком или до её падения).
        msgs = skill_advisor._collect_conversations(str(agent_dir), days=7)
        # Файл читается одним куском и parsing валится — ожидаем []
        assert msgs == []


class TestFormatConversations:
    def test_truncates_to_max_chars(self) -> None:
        msgs = [
            {"timestamp": "2026-04-18T10:00", "role": "user", "content": "x" * 500}
            for _ in range(20)
        ]
        out = skill_advisor._format_conversations(msgs, max_chars=1000)
        assert "обрезано" in out
        assert len(out) < 2000  # защита от взрыва

    def test_preserves_order(self) -> None:
        msgs = [
            {"timestamp": "2026-04-18T10:00", "role": "user", "content": "alpha"},
            {"timestamp": "2026-04-18T10:01", "role": "assistant", "content": "beta"},
        ]
        out = skill_advisor._format_conversations(msgs)
        assert out.index("alpha") < out.index("beta")


class TestGetCurrentSkills:
    def test_empty_dir_returns_empty_string(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        assert skill_advisor._get_current_skills(str(agent_dir)) == ""

    def test_lists_md_files_and_skill_folders(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        skills = agent_dir / "skills"
        skills.mkdir()
        (skills / "quick-skill.md").write_text("x")
        (skills / "deep-skill").mkdir()
        (skills / "deep-skill" / "SKILL.md").write_text("x")
        # Папка без SKILL.md — игнор
        (skills / "orphan").mkdir()

        out = skill_advisor._get_current_skills(str(agent_dir))
        assert "quick-skill" in out
        assert "deep-skill" in out
        assert "orphan" not in out


class TestStoreSuggestions:
    def test_creates_pending_file(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        suggestions = [
            {
                "id": "abc",
                "suggested_skill": {"name": "summarize-docs", "title": "T"},
                "confidence": "high",
            }
        ]
        path = skill_advisor.store_suggestions(str(agent_dir), suggestions)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["suggestions"]) == 1
        assert data["suggestions"][0]["suggested_skill"]["name"] == "summarize-docs"

    def test_deduplicates_by_skill_name(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        first = [{"suggested_skill": {"name": "dup-skill"}}]
        skill_advisor.store_suggestions(str(agent_dir), first)

        # То же имя — не добавляется
        skill_advisor.store_suggestions(str(agent_dir), first)

        data = json.loads(
            (_agent_dir(tmp_path) / "memory" / "skill_suggestions" / "pending.json")
            .read_text(encoding="utf-8")
        )
        assert len(data["suggestions"]) == 1

    def test_appends_new_suggestion(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        skill_advisor.store_suggestions(
            str(agent_dir),
            [{"suggested_skill": {"name": "a"}}],
        )
        skill_advisor.store_suggestions(
            str(agent_dir),
            [{"suggested_skill": {"name": "b"}}],
        )
        data = json.loads(
            (agent_dir / "memory" / "skill_suggestions" / "pending.json")
            .read_text(encoding="utf-8")
        )
        names = {s["suggested_skill"]["name"] for s in data["suggestions"]}
        assert names == {"a", "b"}


class TestReceiveSuggestion:
    def test_valid_message_writes_to_inbox(self, tmp_path: Path) -> None:
        master_dir = _agent_dir(tmp_path, "me")
        msg = FleetMessage(
            source="agent:coder",
            target="skill_inbox:me",
            content=json.dumps({
                "type": "skill_suggestions",
                "agent_name": "coder",
                "suggestions": [{"suggested_skill": {"name": "refactor"}}],
            }),
            msg_type=MessageType.SYSTEM,
        )
        assert skill_advisor.receive_suggestion(str(master_dir), msg) is True

        date_str = datetime.now().strftime("%Y-%m-%d")
        inbox = master_dir / "memory" / skill_advisor.INBOX_DIR / f"coder_{date_str}.json"
        assert inbox.exists()
        data = json.loads(inbox.read_text(encoding="utf-8"))
        assert len(data) == 1

    def test_invalid_json_returns_false(self, tmp_path: Path) -> None:
        master_dir = _agent_dir(tmp_path)
        msg = FleetMessage(
            source="agent:coder",
            target="skill_inbox:me",
            content="not json at all {",
            msg_type=MessageType.SYSTEM,
        )
        assert skill_advisor.receive_suggestion(str(master_dir), msg) is False

    def test_empty_suggestions_returns_false(self, tmp_path: Path) -> None:
        master_dir = _agent_dir(tmp_path)
        msg = FleetMessage(
            source="agent:coder",
            target="skill_inbox:me",
            content=json.dumps({"agent_name": "coder", "suggestions": []}),
            msg_type=MessageType.SYSTEM,
        )
        assert skill_advisor.receive_suggestion(str(master_dir), msg) is False

    def test_multiple_messages_append_to_same_day_file(
        self, tmp_path: Path
    ) -> None:
        master_dir = _agent_dir(tmp_path)
        for skill_name in ["a", "b", "c"]:
            msg = FleetMessage(
                source="agent:coder",
                target="skill_inbox:me",
                content=json.dumps({
                    "agent_name": "coder",
                    "suggestions": [{"suggested_skill": {"name": skill_name}}],
                }),
                msg_type=MessageType.SYSTEM,
            )
            skill_advisor.receive_suggestion(str(master_dir), msg)

        date_str = datetime.now().strftime("%Y-%m-%d")
        inbox = master_dir / "memory" / skill_advisor.INBOX_DIR / f"coder_{date_str}.json"
        data = json.loads(inbox.read_text(encoding="utf-8"))
        assert len(data) == 3


class TestCompileDailyDigest:
    def test_returns_none_when_no_inbox(self, tmp_path: Path) -> None:
        master_dir = _agent_dir(tmp_path)
        assert skill_advisor.compile_daily_digest(str(master_dir)) is None

    def test_returns_none_when_inbox_empty(self, tmp_path: Path) -> None:
        master_dir = _agent_dir(tmp_path)
        (master_dir / "memory" / skill_advisor.INBOX_DIR).mkdir(parents=True)
        assert skill_advisor.compile_daily_digest(str(master_dir)) is None

    def test_builds_digest_and_archives(self, tmp_path: Path) -> None:
        master_dir = _agent_dir(tmp_path)
        inbox = master_dir / "memory" / skill_advisor.INBOX_DIR
        inbox.mkdir(parents=True)
        (inbox / "coder_2026-04-18.json").write_text(
            json.dumps([
                {
                    "agent_name": "coder",
                    "pattern": "frequent refactors",
                    "frequency": 5,
                    "confidence": "high",
                    "suggested_skill": {
                        "name": "refactor-helper",
                        "title": "Refactor Helper",
                        "description": "Help with refactoring",
                        "capabilities": ["extract method", "rename"],
                    },
                    "examples": ["example 1"],
                }
            ]),
            encoding="utf-8",
        )

        digest = skill_advisor.compile_daily_digest(str(master_dir))
        assert digest is not None
        assert "Refactor Helper" in digest
        assert "refactor-helper" in digest
        assert "frequent refactors" in digest
        # Файл перемещён в архив
        archive = master_dir / "memory" / skill_advisor.ARCHIVE_DIR
        assert archive.exists()
        assert any(p.name.endswith("coder_2026-04-18.json") for p in archive.iterdir())
        # Inbox пуст
        assert not any(inbox.glob("*.json"))

    def test_skips_non_list_json(self, tmp_path: Path) -> None:
        master_dir = _agent_dir(tmp_path)
        inbox = master_dir / "memory" / skill_advisor.INBOX_DIR
        inbox.mkdir(parents=True)
        (inbox / "bad.json").write_text(json.dumps({"not": "list"}), encoding="utf-8")

        assert skill_advisor.compile_daily_digest(str(master_dir)) is None


class TestReportToMaster:
    @pytest.mark.asyncio
    async def test_empty_suggestions_returns_false(self) -> None:
        bus = FleetBus()
        ok = await skill_advisor.report_to_master("coder", [], bus)
        assert ok is False

    @pytest.mark.asyncio
    async def test_publishes_to_skill_inbox_queue(self) -> None:
        bus = FleetBus()
        q = bus.subscribe("skill_inbox:me")

        ok = await skill_advisor.report_to_master(
            "coder",
            [{"suggested_skill": {"name": "refactor"}}],
            bus,
            master_name="me",
        )
        assert ok is True
        msg = q.get_nowait()
        assert msg.metadata["type"] == "skill_suggestions"
        payload = json.loads(msg.content)
        assert payload["agent_name"] == "coder"
        assert len(payload["suggestions"]) == 1

    @pytest.mark.asyncio
    async def test_no_subscribers_returns_false(self) -> None:
        bus = FleetBus()  # ничего не подписано
        ok = await skill_advisor.report_to_master(
            "coder",
            [{"suggested_skill": {"name": "x"}}],
            bus,
        )
        assert ok is False


class TestSkillSuggestionReceiver:
    @pytest.mark.asyncio
    async def test_receiver_saves_incoming_suggestion(
        self, tmp_path: Path
    ) -> None:
        master_dir = _agent_dir(tmp_path)
        bus = FleetBus()
        receiver = skill_advisor.SkillSuggestionReceiver(
            str(master_dir), "me", bus
        )
        task = asyncio.create_task(receiver.run())

        # Дать receiver'у подписаться
        await asyncio.sleep(0.05)
        # Затем publish
        msg = FleetMessage(
            source="agent:coder",
            target="skill_inbox:me",
            content=json.dumps({
                "type": "skill_suggestions",
                "agent_name": "coder",
                "suggestions": [{"suggested_skill": {"name": "pair-review"}}],
            }),
            msg_type=MessageType.SYSTEM,
            metadata={"type": "skill_suggestions", "count": 1},
        )
        await bus.publish(msg)
        # Дать receiver'у обработать
        await asyncio.sleep(0.1)

        date_str = datetime.now().strftime("%Y-%m-%d")
        inbox = master_dir / "memory" / skill_advisor.INBOX_DIR / f"coder_{date_str}.json"
        assert inbox.exists(), list(master_dir.rglob("*"))

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_receiver_ignores_non_skill_messages(
        self, tmp_path: Path
    ) -> None:
        master_dir = _agent_dir(tmp_path)
        bus = FleetBus()
        receiver = skill_advisor.SkillSuggestionReceiver(
            str(master_dir), "me", bus
        )
        task = asyncio.create_task(receiver.run())
        await asyncio.sleep(0.05)

        await bus.publish(FleetMessage(
            source="agent:coder",
            target="skill_inbox:me",
            content="irrelevant",
            msg_type=MessageType.SYSTEM,
            metadata={"type": "something_else"},
        ))
        await asyncio.sleep(0.05)

        inbox = master_dir / "memory" / skill_advisor.INBOX_DIR
        # Ничего не создано
        assert not inbox.exists() or not any(inbox.glob("*.json"))

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestAnalyzePatterns:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_conversations(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        result = await skill_advisor.analyze_patterns(
            str(agent_dir), "coder", days=7
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_too_few_messages(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        _write_conv(agent_dir, today, [
            {"timestamp": "x", "role": "user", "content": "msg"}
            for _ in range(5)  # < 10 — пропускаем
        ])
        result = await skill_advisor.analyze_patterns(
            str(agent_dir), "coder", days=7
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_low_confidence_patterns(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        _write_conv(agent_dir, today, [
            {"timestamp": f"ts{i}", "role": "user", "content": f"msg{i}"}
            for i in range(15)
        ])

        response_json = json.dumps({
            "patterns": [
                {"pattern": "p1", "confidence": "high",
                 "suggested_skill": {"name": "a"}},
                {"pattern": "p2", "confidence": "low",
                 "suggested_skill": {"name": "b"}},  # отфильтровано
                {"pattern": "p3", "confidence": "medium",
                 "suggested_skill": {"name": "c"}},
            ],
        })

        with patch("src.dream._call_claude_simple",
                   new=AsyncMock(return_value=response_json)):
            result = await skill_advisor.analyze_patterns(
                str(agent_dir), "coder", days=7
            )
        names = {s["suggested_skill"]["name"] for s in result}
        assert names == {"a", "c"}
        for s in result:
            assert "id" in s
            assert "timestamp" in s
            assert s["agent_name"] == "coder"

    @pytest.mark.asyncio
    async def test_builtin_prompt_does_not_crash_on_json_braces(
        self, tmp_path: Path
    ) -> None:
        """Regression: _BUILTIN_PROMPT содержит литеральные `{...}` в JSON-
        примере. str.format() на них крашилось — analyze_patterns должен
        переживать это через _substitute."""
        agent_dir = _agent_dir(tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        _write_conv(agent_dir, today, [
            {"timestamp": f"ts{i}", "role": "user", "content": f"msg{i}"}
            for i in range(15)
        ])

        with patch("src.dream._call_claude_simple",
                   new=AsyncMock(return_value='{"patterns": []}')):
            # Любой KeyError тут = регрессия к старому .format() багу.
            result = await skill_advisor.analyze_patterns(
                str(agent_dir), "coder", days=7
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_claude_error_returns_empty(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        _write_conv(agent_dir, today, [
            {"timestamp": f"ts{i}", "role": "user", "content": f"msg{i}"}
            for i in range(15)
        ])

        with patch("src.dream._call_claude_simple",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await skill_advisor.analyze_patterns(
                str(agent_dir), "coder", days=7
            )
        assert result == []
