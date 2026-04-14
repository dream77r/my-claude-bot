"""
GitHub Sync — ночное резервное копирование данных агентов.

Запускается ежедневно в 03:00 UTC.
Копирует память и конфиги всех агентов в приватный GitHub репозиторий.
Поддерживает полную git-историю — можно откатиться на любую дату.

Структура репо:
  clients-backup/
  └── clients/
      └── {founder_id}/          # папка этой установки
          ├── agents/
          │   ├── me/memory/     # память оркестратора
          │   ├── coder/memory/
          │   ├── team/memory/
          │   └── archivist/memory/
          └── README.md
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Папки внутри memory/, которые не нужно бэкапить (runtime-состояние)
_MEMORY_EXCLUDE = {"sessions", "outbox", "dispatch", ".git"}


def _run_git(args: list[str], cwd: Path) -> tuple[bool, str]:
    """Выполнить git команду, вернуть (ok, output)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            logger.debug(f"git {' '.join(args)}: {result.stderr.strip()}")
            return False, output
        return True, output
    except subprocess.TimeoutExpired:
        logger.error(f"git {' '.join(args)}: timeout")
        return False, "timeout"
    except Exception as e:
        logger.error(f"git {' '.join(args)}: {e}")
        return False, str(e)


def _get_client_folder(project_root: Path) -> str:
    """
    Получить имя папки клиента.

    Формат: {founder_id} или {founder_id}_@{username} если ник найден в профиле.
    """
    founder_id = os.getenv("FOUNDER_TELEGRAM_ID", "unknown")

    # Попробовать извлечь @username из profile.md агента me
    username = None
    for profile_path in project_root.glob("agents/*/memory/profile.md"):
        try:
            content = profile_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                match = re.search(r"@([A-Za-z0-9_]{3,})", line)
                if match:
                    username = match.group(1)
                    break
        except Exception:
            pass
        if username:
            break

    return f"{founder_id}_@{username}" if username else str(founder_id)


def _copy_memory(src: Path, dst: Path) -> None:
    """Скопировать папку memory/, исключая runtime-данные."""
    if dst.exists():
        shutil.rmtree(dst)

    def ignore_fn(directory: str, contents: list[str]) -> set[str]:
        result = set()
        for name in contents:
            if name in _MEMORY_EXCLUDE or name.endswith(".lock"):
                result.add(name)
        return result

    shutil.copytree(src, dst, ignore=ignore_fn)


def _copy_config_sanitized(src: Path, dst: Path) -> None:
    """Скопировать agent.yaml с удалением токенов и секретов."""
    try:
        content = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        for key in ("bot_token", "token", "api_key", "secret", "password"):
            content.pop(key, None)
        dst.write_text(
            yaml.dump(content, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Ошибка санитизации {src}: {e}")
        dst.write_text("# config sanitization failed\n", encoding="utf-8")


def _ensure_repo(repo_dir: Path, repo_url: str) -> bool:
    """Клонировать репо если нет, иначе обновить. Вернуть True если успешно."""
    if not (repo_dir / ".git").exists():
        logger.info(f"GitHub Sync: клонирую репо")
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        ok, out = _run_git(["clone", repo_url, str(repo_dir)], cwd=repo_dir.parent)
        if not ok:
            # Репо пустое — clone может упасть, инициализируем вручную
            if "empty" in out.lower() or "did not find" in out.lower():
                repo_dir.mkdir(parents=True, exist_ok=True)
                _run_git(["init"], cwd=repo_dir)
                _run_git(["remote", "add", "origin", repo_url], cwd=repo_dir)
                _run_git(["checkout", "-b", "main"], cwd=repo_dir)
                logger.info("GitHub Sync: инициализировал пустой репо")
                return True
            logger.error(f"GitHub Sync: ошибка клонирования: {out}")
            return False
    else:
        _run_git(["pull", "--rebase", "origin", "main"], cwd=repo_dir)

    return True


def _update_sync_meta(repo_dir: Path, client_folder: str, agents: list[str], timestamp: str) -> None:
    """Обновить .sync-meta.json в корне репо."""
    meta_file = repo_dir / ".sync-meta.json"
    try:
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    except Exception:
        meta = {}
    meta[client_folder] = {"last_sync": timestamp, "agents": agents}
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


async def _collect_agent_data(project_root: Path) -> dict:
    """Собрать данные всех агентов для отправки."""
    agents_dir = project_root / "agents"
    result = {}

    for agent_yaml in sorted(agents_dir.glob("*/agent.yaml")):
        agent_name = agent_yaml.parent.name
        agent_src = agent_yaml.parent
        agent_data: dict = {"memory": {}, "config": ""}

        # Собрать файлы памяти
        memory_src = agent_src / "memory"
        if memory_src.exists():
            for f in memory_src.rglob("*"):
                if f.is_file() and not any(
                    part in _MEMORY_EXCLUDE for part in f.parts
                ) and not f.name.endswith(".lock"):
                    rel = str(f.relative_to(memory_src))
                    try:
                        agent_data["memory"][rel] = f.read_text(encoding="utf-8")
                    except Exception:
                        pass

        # Конфиг (sanitized)
        try:
            content = yaml.safe_load(agent_yaml.read_text(encoding="utf-8")) or {}
            for key in ("bot_token", "token", "api_key", "secret", "password"):
                content.pop(key, None)
            agent_data["config"] = yaml.dump(
                content, allow_unicode=True, default_flow_style=False
            )
        except Exception:
            pass

        result[agent_name] = agent_data

    return result


async def _push_via_http(project_root: Path) -> dict:
    """
    Отправить бэкап через HTTP на центральный сервер владельца.
    Используется клиентскими установками без прямого SSH-доступа к GitHub.
    """
    import httpx

    backup_url = os.getenv("BACKUP_URL", "").rstrip("/")
    backup_secret = os.getenv("BACKUP_SECRET", "")

    if not backup_url or not backup_secret:
        return {"success": False, "message": "BACKUP_URL или BACKUP_SECRET не заданы"}

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    agents = await _collect_agent_data(project_root)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{backup_url}/api/backup/push",
                json={
                    "secret": backup_secret,
                    "agents": agents,
                    "timestamp": timestamp,
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "success": True,
                "message": "Бэкап отправлен на сервер",
                "timestamp": timestamp,
                "repo_url": data.get("backup_url", ""),
            }
        return {
            "success": False,
            "message": f"Ошибка сервера: {resp.status_code} {resp.text[:200]}",
        }
    except Exception as e:
        return {"success": False, "message": f"Ошибка подключения: {e}"}


async def _auto_register(project_root: Path) -> bool:
    """
    Автоматически зарегистрироваться на сервере бэкапов при первом запуске.
    Сохраняет BACKUP_SECRET в .env.
    Возвращает True если успешно.
    """
    import httpx
    import socket

    backup_url = os.getenv("BACKUP_URL", "").rstrip("/")
    if not backup_url:
        return False

    founder_id = os.getenv("FOUNDER_TELEGRAM_ID", "unknown")

    # Попробовать извлечь username из профиля
    username = None
    for profile_path in project_root.glob("agents/*/memory/profile.md"):
        try:
            content = profile_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                match = re.search(r"@([A-Za-z0-9_]{3,})", line)
                if match:
                    username = match.group(1)
                    break
        except Exception:
            pass
        if username:
            break

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{backup_url}/api/backup/register",
                json={
                    "founder_id": founder_id,
                    "username": username,
                    "hostname": socket.gethostname(),
                },
            )
        if resp.status_code != 200:
            logger.warning(f"Auto-register failed: {resp.status_code}")
            return False

        data = resp.json()
        secret = data.get("secret")
        if not secret:
            return False

        # Записать BACKUP_SECRET в .env
        env_file = project_root / ".env"
        env_content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        if "BACKUP_SECRET=" not in env_content:
            with open(env_file, "a") as f:
                f.write(f"\nBACKUP_SECRET={secret}\n")
        else:
            import re as _re
            env_content = _re.sub(
                r"BACKUP_SECRET=.*", f"BACKUP_SECRET={secret}", env_content
            )
            env_file.write_text(env_content, encoding="utf-8")

        # Обновить текущий процесс
        os.environ["BACKUP_SECRET"] = secret

        logger.info(f"Auto-registered with backup server. URL: {data.get('backup_url')}")
        return True

    except Exception as e:
        logger.warning(f"Auto-register error: {e}")
        return False


async def run_github_sync(project_root: Path) -> dict:
    """
    Выполнить синхронизацию с GitHub.

    Два режима:
    - Прямой SSH (владелец): если GITHUB_SYNC_REPO задан и SSH доступен
    - HTTP API (клиент): если BACKUP_URL задан

    Returns:
        dict: success, message, timestamp, [client_folder, agents, repo_url]
    """
    # Если задан BACKUP_URL — клиентский режим через HTTP
    if os.getenv("BACKUP_URL"):
        # Авторегистрация при первом запуске
        if not os.getenv("BACKUP_SECRET"):
            await _auto_register(project_root)
        if os.getenv("BACKUP_SECRET"):
            return await _push_via_http(project_root)
        return {"success": False, "message": "Регистрация на сервере бэкапов не удалась"}

    # Иначе — прямой SSH режим (владелец)
    repo_slug = os.getenv("GITHUB_SYNC_REPO", "dream77r/mcb-clients-backup-")

    if not repo_slug:
        return {
            "success": False,
            "message": "Не задан GITHUB_SYNC_REPO в .env",
        }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    repo_url = f"git@github.com:{repo_slug}.git"
    repo_public_url = f"https://github.com/{repo_slug}"

    sync_cache = project_root / ".cache" / "github-sync"
    repo_dir = sync_cache / "repo"

    # Клонировать или обновить репо (в потоке — блокирующий I/O)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, _ensure_repo, repo_dir, repo_url)
    if not ok:
        return {"success": False, "message": "Не удалось подключиться к GitHub репо"}

    client_folder = _get_client_folder(project_root)
    client_dir = repo_dir / "clients" / client_folder
    client_dir.mkdir(parents=True, exist_ok=True)

    # Настроить git identity (нужна для коммита)
    _run_git(["config", "user.email", "github-sync@mcb"], cwd=repo_dir)
    _run_git(["config", "user.name", "MCB GitHub Sync"], cwd=repo_dir)

    # Скопировать данные агентов
    agents_dir = project_root / "agents"
    copied_agents = []

    def _copy_all_agents() -> list[str]:
        names = []
        for agent_yaml in sorted(agents_dir.glob("*/agent.yaml")):
            agent_name = agent_yaml.parent.name
            agent_src = agent_yaml.parent
            agent_dst = client_dir / "agents" / agent_name
            agent_dst.mkdir(parents=True, exist_ok=True)

            memory_src = agent_src / "memory"
            if memory_src.exists():
                _copy_memory(memory_src, agent_dst / "memory")

            _copy_config_sanitized(agent_yaml, agent_dst / "config.yaml")
            names.append(agent_name)
            logger.debug(f"GitHub Sync: скопирован агент '{agent_name}'")
        return names

    copied_agents = await loop.run_in_executor(None, _copy_all_agents)

    # README клиента
    readme = client_dir / "README.md"
    readme.write_text(
        f"# MCB Backup — {client_folder}\n\n"
        f"Последний бэкап: {timestamp}\n\n"
        f"Агенты: {', '.join(copied_agents)}\n\n"
        f"## Восстановление\n\n"
        f"1. Скопируйте папку `agents/` в новую установку бота\n"
        f"2. Перезапустите бот\n\n"
        f"Для отката на определённую дату: `git log` → `git checkout <hash>`\n",
        encoding="utf-8",
    )

    await loop.run_in_executor(
        None, _update_sync_meta, repo_dir, client_folder, copied_agents, timestamp
    )

    def _commit_and_push() -> tuple[bool, str]:
        _run_git(["add", "-A"], cwd=repo_dir)
        ok, out = _run_git(
            ["commit", "-m", f"daily backup: {client_folder} [{timestamp}]"],
            cwd=repo_dir,
        )
        if not ok and "nothing to commit" in out:
            return True, "no_changes"

        # Push с retry
        for attempt in range(3):
            ok, out = _run_git(["push", "-u", "origin", "main"], cwd=repo_dir)
            if ok:
                return True, "pushed"
            if attempt < 2:
                import time
                time.sleep(30)
        return False, out

    ok, result_code = await loop.run_in_executor(None, _commit_and_push)

    if not ok:
        return {"success": False, "message": f"Ошибка push в GitHub: {result_code}"}

    client_url = f"{repo_public_url}/tree/main/clients/{client_folder}"
    msg = "Нет изменений с прошлого бэкапа" if result_code == "no_changes" else "Бэкап создан"

    logger.info(f"GitHub Sync: {msg} ({client_folder})")
    return {
        "success": True,
        "message": msg,
        "timestamp": timestamp,
        "client_folder": client_folder,
        "agents": copied_agents,
        "repo_url": client_url,
    }


def get_last_sync_info(project_root: Path) -> dict | None:
    """Прочитать информацию о последней синхронизации из кэша."""
    meta_file = project_root / ".cache" / "github-sync" / "repo" / ".sync-meta.json"
    if not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text())
        client_folder = _get_client_folder(project_root)
        return meta.get(client_folder)
    except Exception:
        return None


def get_backup_url(project_root: Path) -> str | None:
    """Получить публичную ссылку на папку клиента в GitHub."""
    repo_slug = os.getenv("GITHUB_SYNC_REPO", "dream77r/mcb-clients-backup-")
    if not repo_slug:
        return None
    client_folder = _get_client_folder(project_root)
    return f"https://github.com/{repo_slug}/tree/main/clients/{client_folder}"


async def github_sync_loop(
    project_root: Path,
    run_hour: int = 3,
    run_minute: int = 0,
) -> None:
    """
    Бесконечный цикл: запускает синхронизацию каждую ночь в run_hour:run_minute UTC.
    При первом запуске выполняет бэкап сразу (для применения к существующим пользователям).
    """
    repo_slug = os.getenv("GITHUB_SYNC_REPO", "")

    if not repo_slug:
        logger.info(
            "GitHub Sync: не настроен (нужен GITHUB_SYNC_REPO в .env). "
            "Синхронизация отключена."
        )
        return

    logger.info(f"GitHub Sync loop запущен: ежедневно в {run_hour:02d}:{run_minute:02d} UTC")

    # Первый запуск — сразу бэкапим (применяется к существующим пользователям)
    first_run_delay = 30  # подождать 30 секунд пока агенты поднимутся
    await asyncio.sleep(first_run_delay)

    result = await run_github_sync(project_root)
    if result["success"]:
        logger.info(f"GitHub Sync (initial): {result['message']}")
    else:
        logger.warning(f"GitHub Sync (initial) failed: {result['message']}")

    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=run_hour, minute=run_minute, second=0, microsecond=0)
            if now >= target:
                target = target + timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            logger.info(
                f"GitHub Sync: следующий запуск через {wait_seconds / 3600:.1f}ч "
                f"({target.strftime('%Y-%m-%d %H:%M UTC')})"
            )
            await asyncio.sleep(wait_seconds)

            result = await run_github_sync(project_root)
            if result["success"]:
                logger.info(f"GitHub Sync: {result['message']}")
            else:
                logger.error(f"GitHub Sync failed: {result['message']}")
                # Retry через 30 минут
                await asyncio.sleep(1800)
                result = await run_github_sync(project_root)
                if not result["success"]:
                    logger.error(f"GitHub Sync retry failed: {result['message']}")

        except asyncio.CancelledError:
            logger.info("GitHub Sync loop остановлен")
            break
        except Exception as e:
            logger.error(f"GitHub Sync loop error: {e}")
            await asyncio.sleep(3600)
