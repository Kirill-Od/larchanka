"""Универсальный реестр плагинов с автозагрузкой пакета.

Плагин объявляется декоратором @registry.register("имя") в своём модуле.
Модули пакета импортируются автоматически, поэтому чтобы добавить плагин,
достаточно положить файл в bot/providers/ или bot/transports/ — править
код ядра не нужно.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Callable, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class PluginError(Exception):
    """Плагин не найден или не смог загрузиться."""


class Registry(Generic[T]):
    def __init__(self, kind: str):
        self._kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def decorator(cls: type[T]) -> type[T]:
            key = name.strip().lower()
            if key in self._items:
                raise PluginError(f"{self._kind} {key!r} уже зарегистрирован")
            self._items[key] = cls
            logger.debug("Зарегистрирован %s: %s", self._kind, key)
            return cls

        return decorator

    def names(self) -> list[str]:
        return sorted(self._items)

    def get(self, name: str) -> type[T]:
        key = name.strip().lower()
        if key not in self._items:
            raise PluginError(
                f"{self._kind} {name!r} не найден. Доступны: {', '.join(self.names()) or '—'}"
            )
        return self._items[key]

    def load_package(self, package_name: str) -> None:
        """Импортирует все модули пакета — при импорте они себя регистрируют."""
        package = importlib.import_module(package_name)
        for module in pkgutil.iter_modules(package.__path__):
            if module.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{package_name}.{module.name}")
            except Exception:
                # Сломанный плагин не должен ронять всё приложение.
                logger.exception("Не удалось загрузить плагин %s", module.name)
