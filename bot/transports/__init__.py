"""Плагины транспорта Telegram: polling и webhook."""

from __future__ import annotations

from bot.core.contracts import Transport
from bot.core.registry import Registry

registry: Registry[Transport] = Registry("транспорт")
register = registry.register


def load_all() -> None:
    registry.load_package(__name__)


def create(name: str, *args, **kwargs) -> Transport:
    load_all()
    return registry.get(name)(*args, **kwargs)


def available() -> list[str]:
    load_all()
    return registry.names()
