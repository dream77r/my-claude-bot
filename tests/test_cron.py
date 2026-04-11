"""Тесты для cron.py."""

from datetime import datetime

import pytest

from src.cron import CronJob, load_cron_jobs, parse_cron_field, should_run


class TestParseCronField:
    def test_wildcard(self):
        assert parse_cron_field("*", 5, 59) is True
        assert parse_cron_field("*", 0, 59) is True

    def test_exact(self):
        assert parse_cron_field("5", 5, 59) is True
        assert parse_cron_field("5", 6, 59) is False

    def test_step(self):
        assert parse_cron_field("*/15", 0, 59) is True
        assert parse_cron_field("*/15", 15, 59) is True
        assert parse_cron_field("*/15", 30, 59) is True
        assert parse_cron_field("*/15", 7, 59) is False

    def test_range(self):
        assert parse_cron_field("9-17", 9, 23) is True
        assert parse_cron_field("9-17", 12, 23) is True
        assert parse_cron_field("9-17", 17, 23) is True
        assert parse_cron_field("9-17", 8, 23) is False
        assert parse_cron_field("9-17", 18, 23) is False

    def test_list(self):
        assert parse_cron_field("1,3,5", 3, 6) is True
        assert parse_cron_field("1,3,5", 2, 6) is False


class TestShouldRun:
    def test_every_minute(self):
        now = datetime(2026, 4, 10, 14, 30)
        assert should_run("* * * * *", now) is True

    def test_specific_time(self):
        now = datetime(2026, 4, 10, 9, 0)
        assert should_run("0 9 * * *", now) is True
        assert should_run("0 10 * * *", now) is False

    def test_monday_9am(self):
        # 2026-04-13 is Monday
        monday = datetime(2026, 4, 13, 9, 0)
        assert should_run("0 9 * * 1", monday) is True

        tuesday = datetime(2026, 4, 14, 9, 0)
        assert should_run("0 9 * * 1", tuesday) is False

    def test_every_15_minutes(self):
        assert should_run("*/15 * * * *", datetime(2026, 1, 1, 12, 0)) is True
        assert should_run("*/15 * * * *", datetime(2026, 1, 1, 12, 15)) is True
        assert should_run("*/15 * * * *", datetime(2026, 1, 1, 12, 7)) is False

    def test_invalid_schedule(self):
        assert should_run("invalid", datetime(2026, 1, 1)) is False
        assert should_run("* * *", datetime(2026, 1, 1)) is False


class TestLoadCronJobs:
    def test_load_valid(self):
        config = {
            "cron": [
                {
                    "name": "test_job",
                    "schedule": "0 9 * * *",
                    "prompt": "Do something",
                    "model": "haiku",
                    "notify": True,
                }
            ]
        }
        jobs = load_cron_jobs(config)
        assert len(jobs) == 1
        assert jobs[0].name == "test_job"
        assert jobs[0].model == "haiku"

    def test_load_defaults(self):
        config = {
            "cron": [
                {
                    "name": "minimal",
                    "schedule": "* * * * *",
                    "prompt": "test",
                }
            ]
        }
        jobs = load_cron_jobs(config)
        assert jobs[0].model == "sonnet"
        assert jobs[0].notify is True

    def test_skip_invalid(self):
        config = {
            "cron": [
                {"name": "missing_fields"},  # нет schedule и prompt
                {
                    "name": "valid",
                    "schedule": "* * * * *",
                    "prompt": "ok",
                },
            ]
        }
        jobs = load_cron_jobs(config)
        assert len(jobs) == 1
        assert jobs[0].name == "valid"

    def test_no_cron_section(self):
        jobs = load_cron_jobs({})
        assert jobs == []

    def test_multiple_jobs(self):
        config = {
            "cron": [
                {"name": "a", "schedule": "0 9 * * 1", "prompt": "weekly"},
                {"name": "b", "schedule": "0 21 * * *", "prompt": "daily"},
            ]
        }
        jobs = load_cron_jobs(config)
        assert len(jobs) == 2
