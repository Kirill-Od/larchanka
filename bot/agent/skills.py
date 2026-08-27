"""Скиллы — текстовые инструкции для агента в обычных .md файлах.

Скилл не выполняется кодом: это инструкция, которую агент читает и выполняет
сам, имеющимися инструментами. Добавить скилл = положить .md в skills/,
код при этом не меняется.

Экономия контекста (progressive disclosure): в системный промпт уходит только
каталог «имя — описание», а тело скилла модель забирает инструментом skill,
когда задача действительно на него похожа. Для модели на 1.7B это критично.

Формат файла:

    ---
    name: morning-briefing
    description: утренняя сводка: погода, дела на день, итог
    ---

    1. ...
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("agent.skills")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SKILLS_DIR = PROJECT_ROOT / "skills"

#: Ограничение на тело одного скилла: инструкция должна влезать в контекст.
MAX_SKILL_CHARS = 6000


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Минимальный YAML-frontmatter: только `ключ: значение` до закрывающих ---."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1:]).strip()
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip()
    # Закрывающего --- нет — считаем, что frontmatter'а не было вовсе.
    return {}, text


def _load_file(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Скилл %s не прочитан: %s", path.name, exc)
        return None

    meta, body = _parse_frontmatter(text)
    body = body.strip()
    if not body:
        logger.warning("Скилл %s пуст, пропускаю", path.name)
        return None

    description = meta.get("description", "")
    if not description:
        # Без описания скилл невидим для модели — берём первую строку тела.
        description = next(
            (ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), ""
        )
    return Skill(
        name=meta.get("name") or path.stem,
        description=description,
        body=body[:MAX_SKILL_CHARS],
        path=path,
    )


class SkillLibrary:
    """Скиллы, загруженные с диска. Читается один раз при старте процесса."""

    def __init__(self, skills: dict[str, Skill], directory: Path):
        self._skills = skills
        self.directory = directory

    @classmethod
    def load(cls, directory: str | Path | None = None) -> "SkillLibrary":
        path = Path(directory) if directory else DEFAULT_SKILLS_DIR
        skills: dict[str, Skill] = {}
        if path.is_dir():
            for file in sorted(path.glob("*.md")):
                skill = _load_file(file)
                if skill is None:
                    continue
                if skill.name in skills:
                    logger.warning("Скилл %s объявлен дважды, беру первый", skill.name)
                    continue
                skills[skill.name] = skill
        else:
            logger.info("Каталог скиллов %s не найден — работаю без них", path)
        return cls(skills, path)

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill | None:
        key = name.strip().lower().removesuffix(".md")
        for skill_name, skill in self._skills.items():
            if skill_name.lower() == key:
                return skill
        return None

    def catalog(self) -> str:
        """Строки «имя — описание» для системного промпта."""
        return "\n".join(f"- {s.name} — {s.description}" for s in self)
