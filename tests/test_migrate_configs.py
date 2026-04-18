"""Tests for scripts/extract_local_config.py + migrate_agent_configs.sh.

Фикстура `git_repo` поднимает изолированный временный репозиторий с fake
remote, чтобы тестировать `git show origin/main:...` и `git diff` без
обращения к настоящему remote.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# Импорт скрипта как модуля по пути (scripts/ не пакет — чтобы не тащить
# __init__.py и не менять упаковку).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "extract_local_config", SCRIPTS_DIR / "extract_local_config.py"
)
extract_local_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_local_config)

MIGRATE_SCRIPT = SCRIPTS_DIR / "migrate_agent_configs.sh"


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=False, env=env
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Создаёт изолированный git repo с fake remote 'origin' и базовым коммитом.

    Структура:
        tmp_path/
          remote/            # bare repo, будет origin'ом
          work/              # рабочая копия, из которой тестируем
            agents/team/agent.yaml
    """
    # Отключаем глобальный gitconfig/hooks — тесты не должны от них зависеть.
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_AUTHOR_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = "t@t"
    env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_COMMITTER_EMAIL"] = "t@t"

    remote = tmp_path / "remote"
    work = tmp_path / "work"
    remote.mkdir()
    # Bare repo для origin.
    _run(["git", "init", "--bare", "-b", "main", str(remote)], cwd=tmp_path, env=env)

    # Рабочая копия: инициализируем, создаём agent.yaml, пушим в remote.
    work.mkdir()
    _run(["git", "init", "-b", "main"], cwd=work, env=env)
    _run(["git", "remote", "add", "origin", str(remote)], cwd=work, env=env)

    agent_dir = work / "agents" / "team"
    agent_dir.mkdir(parents=True)
    base_yaml = {
        "name": "team",
        "bot_token": "${TEAM_BOT_TOKEN}",
        "claude_model": "sonnet",
        "allowed_users": [],
        "sandbox": {"enabled": True, "bubblewrap": True},
    }
    (agent_dir / "agent.yaml").write_text(
        yaml.dump(base_yaml, allow_unicode=True), encoding="utf-8"
    )
    _run(["git", "add", "-A"], cwd=work, env=env)
    _run(["git", "commit", "-m", "initial"], cwd=work, env=env)
    _run(["git", "push", "origin", "main"], cwd=work, env=env)
    _run(["git", "fetch", "origin"], cwd=work, env=env)

    return {"work": work, "remote": remote, "env": env, "base_yaml": base_yaml}


# ── diff_overlay (чистая функция) ──


class TestDiffOverlay:
    def test_scalar_diff(self):
        base = {"model": "sonnet", "name": "team"}
        local = {"model": "opus", "name": "team"}
        assert extract_local_config.diff_overlay(base, local) == {"model": "opus"}

    def test_list_replace(self):
        base = {"allowed_users": []}
        local = {"allowed_users": [42]}
        assert extract_local_config.diff_overlay(base, local) == {"allowed_users": [42]}

    def test_nested_dict_minimal(self):
        base = {"sandbox": {"enabled": True, "bubblewrap": True}}
        local = {"sandbox": {"enabled": True, "bubblewrap": False}}
        assert extract_local_config.diff_overlay(base, local) == {
            "sandbox": {"bubblewrap": False}
        }

    def test_no_diff_returns_empty(self):
        base = {"a": 1, "b": {"c": 2}}
        local = {"a": 1, "b": {"c": 2}}
        assert extract_local_config.diff_overlay(base, local) == {}

    def test_new_field_in_local_included(self):
        base = {"a": 1}
        local = {"a": 1, "custom": "yes"}
        assert extract_local_config.diff_overlay(base, local) == {"custom": "yes"}

    def test_nested_partial_diff_keeps_only_changed(self):
        base = {"sandbox": {"enabled": True, "bubblewrap": True, "nested": False}}
        local = {"sandbox": {"enabled": True, "bubblewrap": False, "nested": False}}
        assert extract_local_config.diff_overlay(base, local) == {
            "sandbox": {"bubblewrap": False}
        }


# ── extract() — интеграция с git ──


class TestExtractLocalConfig:
    def test_writes_overlay_with_only_changed_fields(self, git_repo):
        work = git_repo["work"]
        yaml_path = work / "agents" / "team" / "agent.yaml"
        # Юзер правит конфиг в working tree.
        modified = dict(git_repo["base_yaml"])
        modified["claude_model"] = "opus"
        modified["allowed_users"] = [44117786]
        yaml_path.write_text(yaml.dump(modified, allow_unicode=True), encoding="utf-8")

        # Запуск extract — но из корня репо (функция сама найдёт git root).
        old_cwd = Path.cwd()
        os.chdir(work)
        try:
            status, local_path = extract_local_config.extract(yaml_path, branch="main")
        finally:
            os.chdir(old_cwd)

        assert status == "written"
        assert local_path.exists()
        overlay = yaml.safe_load(local_path.read_text(encoding="utf-8"))
        assert overlay == {"claude_model": "opus", "allowed_users": [44117786]}

    def test_skip_when_local_exists(self, git_repo):
        work = git_repo["work"]
        yaml_path = work / "agents" / "team" / "agent.yaml"
        (yaml_path.parent / "agent.local.yaml").write_text("claude_model: opus\n", encoding="utf-8")

        old_cwd = Path.cwd()
        os.chdir(work)
        try:
            status, _ = extract_local_config.extract(yaml_path, branch="main")
        finally:
            os.chdir(old_cwd)
        assert status == "skip-exists"

    def test_skip_when_no_diff(self, git_repo):
        # Working tree идентичен upstream — overlay не нужен.
        work = git_repo["work"]
        yaml_path = work / "agents" / "team" / "agent.yaml"

        old_cwd = Path.cwd()
        os.chdir(work)
        try:
            status, _ = extract_local_config.extract(yaml_path, branch="main")
        finally:
            os.chdir(old_cwd)
        assert status == "skip-no-diff"
        assert not (yaml_path.parent / "agent.local.yaml").exists()

    def test_preserves_raw_env_var_literals(self, git_repo):
        # Юзер мог заменить токен в yaml'е на литерал (не рекомендуется, но
        # встречается). Или наоборот — изменить шаблон ${X} на ${Y}. Проверим
        # что expandvars НЕ применяется — иначе в overlay попадёт раскрытое
        # значение, и оно затрётся при следующем деплое.
        work = git_repo["work"]
        yaml_path = work / "agents" / "team" / "agent.yaml"
        modified = dict(git_repo["base_yaml"])
        modified["bot_token"] = "${CUSTOM_LOCAL_TOKEN}"
        yaml_path.write_text(yaml.dump(modified, allow_unicode=True), encoding="utf-8")

        old_cwd = Path.cwd()
        os.chdir(work)
        try:
            extract_local_config.extract(yaml_path, branch="main")
        finally:
            os.chdir(old_cwd)

        overlay = yaml.safe_load(
            (yaml_path.parent / "agent.local.yaml").read_text(encoding="utf-8")
        )
        assert overlay == {"bot_token": "${CUSTOM_LOCAL_TOKEN}"}


# ── migrate_agent_configs.sh — end-to-end ──


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestMigrateScript:
    def test_full_migration_flow(self, git_repo):
        """Правим два агента, запускаем shell-скрипт, проверяем overlay+rollback."""
        work = git_repo["work"]
        env = git_repo["env"]

        # Добавляем второго агента в remote (чтобы их было два).
        coder_dir = work / "agents" / "coder"
        coder_dir.mkdir()
        coder_base = {"name": "coder", "claude_model": "haiku"}
        (coder_dir / "agent.yaml").write_text(
            yaml.dump(coder_base), encoding="utf-8"
        )
        _run(["git", "add", "-A"], cwd=work, env=env)
        _run(["git", "commit", "-m", "add coder"], cwd=work, env=env)
        _run(["git", "push", "origin", "main"], cwd=work, env=env)
        _run(["git", "fetch", "origin"], cwd=work, env=env)

        # Юзер правит оба yaml'а.
        team_yaml = work / "agents" / "team" / "agent.yaml"
        team_config = dict(git_repo["base_yaml"])
        team_config["claude_model"] = "opus"
        team_yaml.write_text(yaml.dump(team_config), encoding="utf-8")

        coder_yaml = coder_dir / "agent.yaml"
        coder_config = dict(coder_base)
        coder_config["allowed_users"] = [99]
        coder_yaml.write_text(yaml.dump(coder_config), encoding="utf-8")

        # Запуск миграции.
        code, out, err = _run(
            ["bash", str(MIGRATE_SCRIPT), "main"], cwd=work, env=env
        )
        assert code == 0, f"stderr={err} stdout={out}"

        # Оба overlay созданы с минимальным diff'ом.
        team_overlay = yaml.safe_load(
            (team_yaml.parent / "agent.local.yaml").read_text(encoding="utf-8")
        )
        assert team_overlay == {"claude_model": "opus"}

        coder_overlay = yaml.safe_load(
            (coder_yaml.parent / "agent.local.yaml").read_text(encoding="utf-8")
        )
        assert coder_overlay == {"allowed_users": [99]}

        # И сами agent.yaml откачены к upstream.
        team_now = yaml.safe_load(team_yaml.read_text(encoding="utf-8"))
        assert team_now["claude_model"] == "sonnet"  # upstream default
        coder_now = yaml.safe_load(coder_yaml.read_text(encoding="utf-8"))
        assert "allowed_users" not in coder_now  # upstream не имел этого поля

    def test_skip_when_overlay_already_exists(self, git_repo):
        """Если agent.local.yaml уже есть — ничего не делаем, даже если yaml отличается."""
        work = git_repo["work"]
        env = git_repo["env"]
        team_yaml = work / "agents" / "team" / "agent.yaml"

        # Юзер уже создал overlay вручную.
        (team_yaml.parent / "agent.local.yaml").write_text(
            "claude_model: haiku\n", encoding="utf-8"
        )
        # И после этого ещё раз поправил agent.yaml (сценарий "недомигрировал").
        cfg = dict(git_repo["base_yaml"])
        cfg["claude_model"] = "opus"
        team_yaml.write_text(yaml.dump(cfg), encoding="utf-8")

        code, _out, _err = _run(
            ["bash", str(MIGRATE_SCRIPT), "main"], cwd=work, env=env
        )
        assert code == 0

        # Overlay НЕ перезаписан (там было haiku, не opus).
        overlay = yaml.safe_load(
            (team_yaml.parent / "agent.local.yaml").read_text(encoding="utf-8")
        )
        assert overlay == {"claude_model": "haiku"}

        # И agent.yaml НЕ откачен (юзер сам разрулит).
        now = yaml.safe_load(team_yaml.read_text(encoding="utf-8"))
        assert now["claude_model"] == "opus"

    def test_noop_when_no_local_changes(self, git_repo):
        """Fresh install: yaml'ы не отличаются от upstream → миграция no-op."""
        work = git_repo["work"]
        env = git_repo["env"]
        team_yaml = work / "agents" / "team" / "agent.yaml"

        code, _out, _err = _run(
            ["bash", str(MIGRATE_SCRIPT), "main"], cwd=work, env=env
        )
        assert code == 0
        assert not (team_yaml.parent / "agent.local.yaml").exists()
