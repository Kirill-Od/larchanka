"""Контракты плагинов. Ядро зависит только от этих абстракций.

Три расширяемых слоя:
  * LLMProvider — «инференс как плагин»: чем именно считать ответ;
  * Transport   — как доставляются апдейты Telegram (long polling или webhook);
  * Tool        — что агент умеет делать руками (exec, чтение скиллов, …).
"""

from __future__ import annotations

import abc
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Обработчик апдейта: транспорт получает dict от Telegram и передаёт его сюда.
UpdateHandler = Callable[[dict[str, Any]], None]

#: Роли сообщений. tool — результат вызова инструмента, его пишет харнесс,
#: а не модель: так в контексте видно, что именно вернула система.
ROLES = ("system", "user", "assistant", "tool")


@dataclass(frozen=True)
class Message:
    """Одна реплика в контексте.

    Уходит между процессами через multiprocessing.Queue, поэтому — простой
    неизменяемый dataclass без ссылок на живые объекты.
    """

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"неизвестная роль {self.role!r}, ожидалась одна из {ROLES}")


class LLMError(Exception):
    """Провайдер не смог выдать ответ."""


class EmptyAnswer(LLMError):
    """Модель отработала, но текста не вернула.

    Отдельный тип, потому что это осечка одного шага, а не отказ провайдера:
    reasoning-модель способна потратить весь бюджет генерации на размышления
    и вернуть пустой content. Харнесс на этом переспрашивает, вместо того
    чтобы выбросить всё, что агент уже собрал инструментами.
    """


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

    def chat(self, messages: Sequence[Message]) -> str:
        """Запрос с историей — то, чем живёт агентный цикл.

        Базовая реализация склеивает диалог в один промпт, поэтому любой
        старый провайдер с одним только generate() работает в агенте
        без единой правки. Провайдеру с настоящим chat-эндпоинтом достаточно
        переопределить этот метод (см. ollama и openai_compat).
        """
        from bot.core.text import render_dialog

        return self.generate(render_dialog(messages))

    def health(self) -> bool:
        """Быстрая проверка доступности. По умолчанию — считаем живым."""
        return True


class Transport(abc.ABC):
    """Плагин доставки апдейтов Telegram."""

    name: str = ""

    @abc.abstractmethod
    def run(self, handler: UpdateHandler, stop: threading.Event) -> None:
        """Блокирующий цикл. Обязан завершиться, когда выставлен stop."""


class ToolError(Exception):
    """Инструмент не смог выполнить вызов.

    Это НЕ авария: текст ошибки возвращается модели как результат вызова,
    чтобы она могла исправиться на следующем шаге.
    """


class Tool(abc.ABC):
    """Плагин действия: то, что агент вызывает по имени.

    Создаётся внутри процесса агента и живёт всё время его работы.
    Как и провайдер, читает свои настройки сам — новый инструмент
    не требует правок config.py.
    """

    #: Имя, которым модель вызывает инструмент.
    name: str = ""
    #: Одна строка для системного промпта — что инструмент делает.
    description: str = ""
    #: Пример вызова, который уходит модели вместе с описанием.
    usage: str = ""

    def __init__(self, settings: Mapping[str, str]):
        self._settings = settings

    @abc.abstractmethod
    def run(self, args: Mapping[str, Any]) -> str:
        """Выполняет вызов и возвращает результат текстом — его увидит модель.

        Бросает ToolError, если аргументы плохие или действие не удалось.
        """
