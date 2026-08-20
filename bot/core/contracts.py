"""Контракты плагинов. Ядро зависит только от этих абстракций.

Два расширяемых слоя:
  * LLMProvider — «инференс как плагин»: чем именно считать ответ;
  * Transport   — как доставляются апдейты Telegram (long polling или webhook).
"""

from __future__ import annotations

import abc
import threading
from collections.abc import Callable, Mapping
from typing import Any

# Обработчик апдейта: транспорт получает dict от Telegram и передаёт его сюда.
UpdateHandler = Callable[[dict[str, Any]], None]


class LLMError(Exception):
    """Провайдер не смог выдать ответ."""


class LLMProvider(abc.ABC):
    """Плагин инференса.

    Реализация обязана быть self-contained: конструктор получает только
    словарь настроек (объединённые .env и окружение), поэтому новый провайдер
    добавляется без правок config.py и остального кода.

    Экземпляр создаётся ВНУТРИ рабочего процесса агента, поэтому держать в нём
    несериализуемые объекты безопасно.
    """

    #: Имя, под которым плагин виден в LLM_PROVIDER.
    name: str = ""

    def __init__(self, settings: Mapping[str, str], timeout: int):
        self._settings = settings
        self._timeout = timeout

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """Человекочитаемое имя модели — для логов и /start."""

    @abc.abstractmethod
    def generate(self, prompt: str) -> str:
        """Один независимый запрос без истории диалога.

        Бросает LLMError с текстом, пригодным для показа пользователю.
        """

    def health(self) -> bool:
        """Быстрая проверка доступности. По умолчанию — считаем живым."""
        return True


class Transport(abc.ABC):
    """Плагин доставки апдейтов Telegram."""

    name: str = ""

    @abc.abstractmethod
    def run(self, handler: UpdateHandler, stop: threading.Event) -> None:
        """Блокирующий цикл. Обязан завершиться, когда выставлен stop."""
