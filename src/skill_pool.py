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
    """
    Один скилл в manifest.json.

    Поддерживает два формата:
    - Single-file (legacy): поле `file` указывает на published/{skill}.md
    - Bundle (agentskills.io): поле `path` указывает на published/{skill}/
      директорию, в которой лежит SKILL.md и опциональные scripts/, references/,
      assets/ (вся директория копируется при установке)
    """

    name: str
    title: str
    description: str
    version: str
    # Один из двух должен быть задан:
    file: str = ""          # legacy: относительный путь к .md
    path: str = ""          # bundle: относительный путь к директории
    type: str = "single"    # "single" | "bundle"
    tags: list[str] = field(default_factory=list)
    requires_memory: list[str] = field(default_factory=list)
    has_scripts: bool = False  # true если в bundle есть .py/.sh/.js/etc
    author: str = ""
    created: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "SkillCatalogEntry":
        # Автоопределение типа: если задан path → bundle, иначе single
        entry_type = data.get("type")
        if not entry_type:
            entry_type = "bundle" if data.get("path") else "single"

        return cls(
            name=name,
            file=data.get("file", ""),
            path=data.get("path", ""),
            type=entry_type,
            title=data.get("title", name),
            description=data.get("description", ""),
            version=data.get("version", "0.0.0"),
            tags=list(data.get("tags") or []),
            requires_memory=list(data.get("requires_memory") or []),
            has_scripts=bool(data.get("has_scripts", False)),
            author=data.get("author", ""),
            created=data.get("created", ""),
        )

    def source_rel_path(self) -> str:
        """
        Вернуть относительный путь к скиллу в пуле (для bundle — папка,
        для single — файл). Bundle имеет приоритет если оба заданы.
        """
        if self.type == "bundle" and self.path:
            return self.path
        return self.file


@dataclass
class InstallResult:
    """Результат установки скилла в агента."""

    ok: bool
    skill_name: str
    installed_to: str = ""
    missing_memory: list[str] = field(default_factory=list)
    has_scripts: bool = False
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

    # Расширения файлов, считающиеся "исполняемыми" — триггер warning при install
    EXECUTABLE_EXTENSIONS = frozenset({
        ".py", ".sh", ".bash", ".zsh",
        ".js", ".mjs", ".cjs", ".ts", ".tsx",
        ".rb", ".pl", ".php", ".lua",
    })

    @classmethod
    def has_executable_scripts(cls, path: Path) -> bool:
        """
        Проверить есть ли в директории исполняемые скрипты.

        Рекурсивно обходит директорию и ищет файлы с расширениями из
        EXECUTABLE_EXTENSIONS. Используется для предупреждения пользователя
        при установке bundle-скиллов со скриптами.

        Args:
            path: путь к директории (если не директория — возвращается False)

        Returns:
            True если найден хотя бы один файл с исполняемым расширением
        """
        if not path.is_dir():
            return False
        for item in path.rglob("*"):
            if item.is_file() and item.suffix.lower() in cls.EXECUTABLE_EXTENSIONS:
                return True
        return False

    def _resolve_source_path(self, entry: SkillCatalogEntry) -> Path:
        """
        Получить абсолютный путь к источнику скилла в пуле.

        Для bundle это директория, для single — файл.
        Валидирует что путь внутри published/.
        """
        rel = entry.source_rel_path()
        if not rel:
            raise SkillPoolError(
                f"Скилл '{entry.name}' не имеет ни file ни path в manifest.json"
            )
        if not rel.startswith("published/"):
            raise SkillPoolError(
                f"Скилл '{entry.name}' не в published/, установка запрещена "
                f"(путь: {rel})"
            )
        source = self.repo_dir / rel
        if not source.exists():
            raise SkillPoolError(f"Источник скилла не найден: {source}")
        return source

    def read_skill_body(self, entry: SkillCatalogEntry) -> str:
        """
        Прочитать основной файл скилла (frontmatter + тело).

        Для single-скилла — содержимое .md файла.
        Для bundle — содержимое {dir}/SKILL.md.

        Raises:
            SkillPoolError если путь невалиден или файл отсутствует
        """
        source = self._resolve_source_path(entry)
        if source.is_dir():
            skill_md = source / "SKILL.md"
            if not skill_md.exists():
                raise SkillPoolError(
                    f"Bundle '{entry.name}' не содержит SKILL.md: {source}"
                )
            return skill_md.read_text(encoding="utf-8")
        return source.read_text(encoding="utf-8")

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

        Поддерживает два формата:
        - Single: копирует .md файл в agents/{name}/skills/{skill}.md
        - Bundle: копирует всю директорию в agents/{name}/skills/{skill}/

        Порядок действий:
        1. Найти скилл в каталоге
        2. Определить источник (файл или директория)
        3. Проверить requires_memory (strict → отказ; soft → warning)
        4. Проверить наличие скриптов в bundle → выставить has_scripts
        5. Скопировать файл/директорию в агента
        6. Вернуть InstallResult

        Args:
            skill_name: имя скилла в каталоге
            agent_dir: корневая директория агента (содержит skills/, memory/)
            overwrite: перезаписать существующее? (default False)
            strict_memory: если True, отсутствующие файлы памяти => ошибка

        Returns:
            InstallResult с ok, installed_to, missing_memory, has_scripts, error
        """
        entry = self.get_skill(skill_name)
        if entry is None:
            return InstallResult(
                ok=False, skill_name=skill_name,
                error=f"Скилл '{skill_name}' не найден в пуле"
            )

        try:
            source = self._resolve_source_path(entry)
        except SkillPoolError as e:
            return InstallResult(
                ok=False, skill_name=skill_name, error=str(e)
            )

        is_bundle = source.is_dir()

        # Валидация bundle: обязательный SKILL.md
        if is_bundle and not (source / "SKILL.md").exists():
            return InstallResult(
                ok=False, skill_name=skill_name,
                error=f"Bundle '{entry.name}' не содержит SKILL.md"
            )

        skills_dir = agent_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        if is_bundle:
            target = skills_dir / entry.name
        else:
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

        # Проверка скриптов (для предупреждения пользователя)
        has_scripts = False
        if is_bundle:
            has_scripts = self.has_executable_scripts(source)

        # Копирование
        if is_bundle:
            if target.exists() and overwrite:
                shutil.rmtree(target)
            shutil.copytree(source, target)
            logger.info(
                f"Установлен bundle '{entry.name}' v{entry.version} в {target}"
                + (f" (с исполняемыми скриптами)" if has_scripts else "")
            )
        else:
            body = source.read_text(encoding="utf-8")
            target.write_text(body, encoding="utf-8")
            logger.info(
                f"Установлен скилл '{entry.name}' v{entry.version} в {target}"
            )

        return InstallResult(
            ok=True,
            skill_name=skill_name,
            installed_to=str(target),
            missing_memory=missing_memory,
            has_scripts=has_scripts,
        )

    def uninstall_skill(self, skill_name: str, agent_dir: Path) -> bool:
        """
        Удалить скилл из агента. Работает с обоими форматами (file и bundle).

        Память агента не трогается — только скилл.

        Returns:
            True если скилл был установлен и удалён
        """
        skills_dir = agent_dir / "skills"

        # Сначала bundle (директория)
        bundle_target = skills_dir / skill_name
        if bundle_target.exists() and bundle_target.is_dir():
            shutil.rmtree(bundle_target)
            logger.info(f"Удалён bundle '{skill_name}' у агента {agent_dir.name}")
            return True

        # Потом single-file
        file_target = skills_dir / f"{skill_name}.md"
        if file_target.exists() and file_target.is_file():
            file_target.unlink()
            logger.info(f"Удалён скилл '{skill_name}' у агента {agent_dir.name}")
            return True

        return False


# Официальный публичный пул скиллов my-claude-bot.
# Используется если SKILL_POOL_URL не задан в .env — это дефолт "из коробки".
# HTTPS (не SSH) чтобы работало без настроенного SSH-ключа у GitHub.
DEFAULT_SKILL_POOL_URL = "https://github.com/dream77r/my-claude-bot-skills.git"


def make_pool_from_env(project_root: Path) -> SkillPool | None:
    """
    Создать SkillPool из переменных окружения или дефолта.

    Читает:
        SKILL_POOL_URL (опционально, fallback — DEFAULT_SKILL_POOL_URL)
        SKILL_POOL_BRANCH (default main)
        SKILL_POOL_CACHE (default {project_root}/.cache/skill-pool)

    Специальное значение SKILL_POOL_URL=disabled — полностью отключает пул
    (возвращается None, команды выдают "пул не настроен").

    Returns:
        SkillPool или None если пул явно отключён через SKILL_POOL_URL=disabled
    """
    url = os.environ.get("SKILL_POOL_URL", "").strip()

    # Явное отключение
    if url.lower() in ("disabled", "off", "none"):
        return None

    # Fallback на дефолтный публичный пул
    if not url:
        url = DEFAULT_SKILL_POOL_URL
        logger.info(
            f"SKILL_POOL_URL не задан, использую дефолтный пул: {url}"
        )

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
