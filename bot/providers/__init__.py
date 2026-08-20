"""Плагины инференса. Файл в этом пакете = новый доступный провайдер."""

from __future__ import annotations

from collections.abc import Mapping

from bot.core.contracts import LLMProvider
from bot.core.registry import Registry

registry: Registry[LLMProvider] = Registry("LLM-провайдер")
register = registry.register


def load_all(extra_packages: str = "") -> None:
    """Загружает встроенные плагины и, опционально, внешние пакеты.

    PLUGIN_PACKAGES позволяет держать провайдер вне этого репозитория:
    достаточно, чтобы пакет был импортируемым.
    """
    registry.load_package(__name__)
    for package in (p.strip() for p in extra_packages.split(",")):
        if package:
            registry.load_package(package)


def create(name: str, settings: Mapping[str, str], timeout: int) -> LLMProvider:
    load_all(settings.get("PLUGIN_PACKAGES", ""))
    return registry.get(name)(settings, timeout)


def available(extra_packages: str = "") -> list[str]:
    load_all(extra_packages)
    return registry.names()
