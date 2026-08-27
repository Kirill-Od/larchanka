"""Инструмент skill: выдаёт агенту полный текст инструкции по имени.

Пара к каталогу скиллов в системном промпте: там только имена и описания,
тело подгружается по требованию — контекст мелкой локальной модели дорог.

К телу скилла приклеивается напоминание, что это инструкция, а не ответ:
без него мелкая модель охотно заполняет шаблон из скилла выдуманными
числами вместо того, чтобы выполнить его шаги инструментами.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bot.agent.skills import SkillLibrary
from bot.core.contracts import Tool, ToolError
from bot.tools import register


#: Приклеивается к любому скиллу: инструкцию надо ВЫПОЛНИТЬ, а не пересказать.
REMINDER = (
    "---\n"
    "ВАЖНО: выше — инструкция, а не готовый ответ. Выполни её шаги по порядку, "
    "по одному вызову инструмента за шаг, начиная с первого. Все данные бери "
    "ТОЛЬКО из результатов вызовов: подставлять правдоподобные значения "
    "в шаблон запрещено. Начни прямо сейчас с первого шага."
)


@register("skill")
class SkillTool(Tool):
    name = "skill"
    description = (
        "читает полный текст скилла (инструкции) по имени. "
        "Вызывай его ПЕРВЫМ, если задача похожа на один из скиллов ниже"
    )
    usage = '{"tool": "skill", "args": {"name": "morning-briefing"}}'

    def __init__(self, settings: Mapping[str, str]):
        super().__init__(settings)
        self._library = SkillLibrary.load(settings.get("SKILLS_DIR", ""))

    def run(self, args: Mapping[str, Any]) -> str:
        name = str(args.get("name") or args.get("skill") or "").strip()
        if not name:
            raise ToolError(
                f"не задан аргумент name. Доступны: {', '.join(self._library.names()) or '—'}"
            )
        skill = self._library.get(name)
        if skill is None:
            raise ToolError(
                f"скилл {name!r} не найден. Доступны: "
                f"{', '.join(self._library.names()) or '—'}"
            )
        return f"Инструкция «{skill.name}»:\n\n{skill.body}\n\n{REMINDER}"
