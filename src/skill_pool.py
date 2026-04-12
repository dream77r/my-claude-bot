"""
Skill Pool — маркетплейс скиллов для my-claude-bot.

Работает с внешним git-репозиторием, который хранит общий каталог скиллов
(например https://github.com/dream77r/my-claude-bot-skills).

Структура пула:
    my-claude-bot-skills/
    ├── manifest.json         каталог метаданных всех published скиллов
    ├── published/            готовые к установке скиллы (автоустановка)
    │   ├── web-research.md
    │   └── ...
    ├── incoming/             карантин — присланные пользователями,
    │                         НЕ устанавливаются автоматически
    └── private/              (опционально, в отдельном приватном репо)

Поведение:
- Клонирование/обновление происходит через git CLI (subprocess)
- Пул кэшируется в .cache/skill-pool/ проекта (gitignored)
- manifest.json читается для листинга и поиска
- Установка копирует файл из published/ в agents/{name}/skills/
- Перед установкой проверяется requires_memory — отсутствующие файлы
  возвращаются вызывающему коду для интерактивного создания

Конфигурация (через agent.yaml или .env):
- SKILL_POOL_URL: git URL публичного пула
- SKILL_POOL_BRANCH: ветка (default main)
- SKILL_POOL_CACHE: локальный путь к кэшу (default .cache/skill-pool)
"""

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SkillCatalogEntry:
    """Один скилл в manifest.json."""

    name: str
    file: str  # относительный путь в репо, напр. "published/web-research.md"
    title: str
    description: str
    version: str
    tags: list[str] = field(default_factory=list)
    requires_memory: list[str] = field(default_factory=list)
    author: str = ""
    created: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "SkillCatalogEntry":
        return cls(
            name=name,
            file=data.get("file", ""),
            title=data.get("title", name),
            description=data.get("description", ""),
            version=data.get("version", "0.0.0"),
            tags=list(data.get("tags") or []),
            requires_memory=list(data.get("requires_memory") or []),
            author=data.get("author", ""),
            created=data.get("created", ""),
        )


@dataclass
class InstallResult:
    """Результат установки скилла в агента."""

    ok: bool
    skill_name: str
    installed_to: str = ""
    missing_memory: list[str] = field(default_factory=list)
    error: str = ""


class SkillPoolError(Exception):
    """Ошибка операций с пулом скиллов."""


class SkillPool:
    """
    Основной класс работы с пулом скиллов.

    Usage:
        pool = SkillPool(
            pool_url="https://github.com/user/my-claude-bot-skills.git",
            cache_dir=Path("/path/to/.cache/skill-pool"),
        )
        pool.refresh()
        skills = pool.list_skills()
        result = pool.install_skill("web-research", agent_dir)
    """

    MANIFEST_FILENAME = "manifest.json"

    def __init__(
        self,
        pool_url: str,
        cache_dir: Path,
        branch: str = "main",
    ):
        """
        Args:
            pool_url: git URL публичного репо с пулом
            cache_dir: локальный путь для клонирования (будет создан)
            branch: ветка (default main)
        """
        if not pool_url:
            raise SkillPoolError("pool_url не может быть пустым")
        self.pool_url = pool_url
        self.cache_dir = Path(cache_dir)
        self.branch = branch

        # Папка куда клонируется сам репо
        repo_name = self._extract_repo_name(pool_url)
        self.repo_dir = self.cache_dir / repo_name

    @staticmethod
    def _extract_repo_name(url: str) -> str:
        """Вытащить имя репо из git URL (xxx.git → xxx)."""
        # Поддерживает https://.../name.git, git@...:name.git, .../name
        last = url.rstrip("/").split("/")[-1]
        if last.endswith(".git"):
            last = last[:-4]
        return last or "skill-pool"

    def _run_git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Запустить git команду, вернуть CompletedProcess."""
        cmd = ["git"] + list(args)
        logger.debug(f"git {' '.join(args)} (cwd={cwd})")
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result

    def refresh(self) -> None:
        """
        Клонировать репо (первый раз) или обновить (git pull).

        Raises:
            SkillPoolError если git вернул ошибку
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        if not self.repo_dir.exists():
            logger.info(f"Клонирую пул скиллов: {self.pool_url} → {self.repo_dir}")
            result = self._run_git(
                "clone",
                "--depth", "1",
                "--branch", self.branch,
                self.pool_url,
                str(self.repo_dir),
            )
            if result.returncode != 0:
                raise SkillPoolError(
                    f"git clone failed: {result.stderr.strip()}"
                )
            return

        # Уже есть — pull
        logger.info(f"Обновляю пул скиллов в {self.repo_dir}")
        result = self._run_git("pull", "--ff-only", cwd=self.repo_dir)
        if result.returncode != 0:
            # Не фатально: может быть detached HEAD или локальные изменения
            logger.warning(
                f"git pull вернул код {result.returncode}: {result.stderr.strip()}"
            )

    def is_available(self) -> bool:
        """Проверить что пул склонирован и содержит manifest."""
        return self.repo_dir.exists() and (self.repo_dir / self.MANIFEST_FILENAME).exists()

    def read_manifest(self) -> dict:
        """
        Прочитать manifest.json.

        Returns:
            dict с полями version, updated, skills{}

        Raises:
            SkillPoolError если файл отсутствует или битый
        """
        manifest_path = self.repo_dir / self.MANIFEST_FILENAME
        if not manifest_path.exists():
            raise SkillPoolError(
                f"manifest.json не найден в {self.repo_dir}. "
                f"Запусти refresh() сначала."
            )
        try:
            with open(manifest_path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise SkillPoolError(f"manifest.json битый: {e}") from e

    def list_skills(self) -> list[SkillCatalogEntry]:
        """
        Вернуть список доступных в пуле скиллов.

        Читает только секцию 'skills' из manifest.json.
        Скиллы в incoming/ не попадают в каталог.
        """
        manifest = self.read_manifest()
        skills_dict = manifest.get("skills") or {}
        entries = [
            SkillCatalogEntry.from_dict(name, data)
            for name, data in skills_dict.items()
        ]
        entries.sort(key=lambda e: e.name)
        return entries

    def get_skill(self, skill_name: str) -> SkillCatalogEntry | None:
        """Найти скилл в каталоге по имени. None если нет."""
        for entry in self.list_skills():
            if entry.name == skill_name:
                return entry
        return None

    def read_skill_body(self, entry: SkillCatalogEntry) -> str:
        """
        Прочитать полный текст файла скилла (frontmatter + тело).

        Args:
            entry: запись из каталога

        Raises:
            SkillPoolError если файл отсутствует или не в published/
        """
        if not entry.file.startswith("published/"):
            raise SkillPoolError(
                f"Скилл '{entry.name}' не в published/, установка запрещена "
                f"(файл: {entry.file})"
            )
        skill_path = self.repo_dir / entry.file
        if not skill_path.exists():
            raise SkillPoolError(
                f"Файл скилла не найден: {skill_path}"
            )
        return skill_path.read_text(encoding="utf-8")

    def check_memory_for_skill(
        self, entry: SkillCatalogEntry, agent_memory_path: Path
    ) -> list[str]:
        """
        Проверить какие файлы памяти из requires_memory отсутствуют у агента.

        Args:
            entry: запись из каталога
            agent_memory_path: путь к папке памяти целевого агента

        Returns:
            Список относительных путей к отсутствующим файлам (пустой — всё ок)
        """
        missing = []
        for rel in entry.requires_memory:
            if not (agent_memory_path / rel).exists():
                missing.append(rel)
        return missing

    def install_skill(
        self,
        skill_name: str,
        agent_dir: Path,
        *,
        overwrite: bool = False,
        strict_memory: bool = False,
    ) -> InstallResult:
        """
        Установить скилл из пула в агента.

        Порядок действий:
        1. Найти скилл в каталоге
        2. Прочитать содержимое файла
        3. Проверить requires_memory (если strict_memory=True — отказ при отсутствии)
        4. Скопировать файл в agents/{name}/skills/{skill_name}.md
        5. Вернуть InstallResult с информацией об отсутствующей памяти

        Args:
            skill_name: имя скилла в каталоге
            agent_dir: корневая директория агента (содержит skills/, memory/)
            overwrite: перезаписать существующий файл? (default False)
            strict_memory: если True, отсутствующие файлы памяти => ошибка

        Returns:
            InstallResult с ok, installed_to, missing_memory, error
        """
        entry = self.get_skill(skill_name)
        if entry is None:
            return InstallResult(
                ok=False, skill_name=skill_name,
                error=f"Скилл '{skill_name}' не найден в пуле"
            )

        try:
            body = self.read_skill_body(entry)
        except SkillPoolError as e:
            return InstallResult(
                ok=False, skill_name=skill_name, error=str(e)
            )

        skills_dir = agent_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        target = skills_dir / f"{entry.name}.md"

        if target.exists() and not overwrite:
            return InstallResult(
                ok=False, skill_name=skill_name,
                error=(
                    f"Скилл '{entry.name}' уже установлен у агента. "
                    f"Используй overwrite=True чтобы заменить."
                ),
            )

        # Проверка памяти
        memory_path = agent_dir / "memory"
        missing_memory = self.check_memory_for_skill(entry, memory_path)

        if missing_memory and strict_memory:
            return InstallResult(
                ok=False,
                skill_name=skill_name,
                missing_memory=missing_memory,
                error=(
                    f"Отсутствуют файлы памяти: {', '.join(missing_memory)}. "
                    f"Создай их перед установкой (strict_memory=True)."
                ),
            )

        # Копирование
        target.write_text(body, encoding="utf-8")
        logger.info(
            f"Установлен скилл '{entry.name}' v{entry.version} в {target}"
        )

        return InstallResult(
            ok=True,
            skill_name=skill_name,
            installed_to=str(target),
            missing_memory=missing_memory,
        )

    def uninstall_skill(self, skill_name: str, agent_dir: Path) -> bool:
        """
        Удалить скилл из агента (только файл, память не трогаем).

        Returns:
            True если скилл был установлен и удалён
        """
        target = agent_dir / "skills" / f"{skill_name}.md"
        if not target.exists():
            return False
        target.unlink()
        logger.info(f"Удалён скилл '{skill_name}' у агента {agent_dir.name}")
        return True


def make_pool_from_env(project_root: Path) -> SkillPool | None:
    """
    Создать SkillPool из переменных окружения или .env.

    Читает:
        SKILL_POOL_URL (обязательный)
        SKILL_POOL_BRANCH (default main)
        SKILL_POOL_CACHE (default {project_root}/.cache/skill-pool)

    Returns:
        SkillPool или None если SKILL_POOL_URL не задан
    """
    url = os.environ.get("SKILL_POOL_URL", "").strip()
    if not url:
        return None
    branch = os.environ.get("SKILL_POOL_BRANCH", "main").strip() or "main"
    cache = os.environ.get("SKILL_POOL_CACHE", "").strip()
    cache_dir = Path(cache) if cache else project_root / ".cache" / "skill-pool"
    return SkillPool(pool_url=url, cache_dir=cache_dir, branch=branch)


def extract_skill_metadata(skill_file: Path) -> dict | None:
    """
    Вытащить frontmatter из файла скилла для попадания в manifest.json.

    Используется при публикации (Phase 4) и для seed-скриптов.

    Returns:
        dict с полями {file, title, description, version, tags, requires_memory,
                       author, created} или None если frontmatter не распарсился
    """
    import re
    text = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None

    return {
        "file": "",  # заполняется вызывающим кодом
        "title": meta.get("name", skill_file.stem).replace("-", " ").title(),
        "description": meta.get("description", ""),
        "version": meta.get("version", "1.0.0"),
        "tags": list(meta.get("tags") or []),
        "requires_memory": list(meta.get("requires_memory") or []),
        "author": meta.get("author", ""),
        "created": meta.get("created", ""),
    }
