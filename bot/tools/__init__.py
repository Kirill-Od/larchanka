"""Плагины действий агента. Файл в этом пакете = новый доступный инструмент.

Устроено так же, как провайдеры и транспорты: декоратор @register("имя"),
автозагрузка модулей пакета, ядро о конкретных инструментах не знает.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from bot.core.contracts import Tool
from bot.core.registry import Registry

logger = logging.getLogger("agent.tools")

registry: Registry[Tool] = Registry("инструмент")
register = registry.register


def load_all(extra_packages: str = "") -> None:
    registry.load_package(__name__)
    for package in (p.strip() for p in extra_packages.split(",")):
        if package:
            registry.load_package(package)


def available(extra_packages: str = "") -> list[str]:
    load_all(extra_packages)
    return registry.names()


def _selected(settings: Mapping[str, str]) -> list[str]:
    """TOOLS_ENABLED (пусто = все) минус TOOLS_DISABLED."""

    def parse(key: str) -> set[str]:
        raw = settings.get(key, "")
        return {chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()}

    names = registry.names()
    enabled = parse("TOOLS_ENABLED") or set(names)
    disabled = parse("TOOLS_DISABLED")
    return [name for name in names if name in enabled and name not in disabled]


def create_all(settings: Mapping[str, str]) -> dict[str, Tool]:
    """Собирает набор инструментов для одного процесса агента.

    Инструмент, который не смог сконфигурироваться, пропускаем: агент должен
    подняться и с урезанным набором, а не падать целиком.
    """
    load_all(settings.get("TOOL_PACKAGES", ""))
    tools: dict[str, Tool] = {}
    for name in _selected(settings):
        try:
            tools[name] = registry.get(name)(settings)
        except Exception:
            logger.exception("Инструмент %s не сконфигурирован, пропускаю", name)
    return tools
