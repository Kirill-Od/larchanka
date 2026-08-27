"""Контекст диалога: чат — это одна длинная лента сообщений.

Живёт в процессе бота, а не в процессе агента: воркеров может быть несколько
и любой из них должен уметь взять любую задачу, поэтому контекст едет с задачей,
а не хранится внутри воркера.

Хранение в памяти. После перезапуска бота контекст пуст — это осознанный
компромисс: диск и шифрование переписки в задачу не входят.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from bot.core.contracts import Message
from bot.core.text import truncate

#: Сколько символов сохраняем от одного сообщения. Вывод exec бывает огромным,
#: и целиком в контексте он не нужен — важен факт и начало вывода.
MAX_STORED_MESSAGE_CHARS = 2000


class ConversationStore:
    """Контексты чатов с обрезкой по объёму. Потокобезопасен: webhook-транспорт
    обрабатывает апдейты в нескольких потоках."""

    def __init__(self, max_messages: int = 40, max_chars: int = 12000):
        self._max_messages = max_messages
        self._max_chars = max_chars
        self._chats: dict[int, list[Message]] = {}
        self._lock = threading.Lock()

    def history(self, chat_id: int) -> tuple[Message, ...]:
        with self._lock:
            return tuple(self._chats.get(chat_id, ()))

    def extend(self, chat_id: int, messages: Sequence[Message]) -> None:
        if not messages:
            return
        with self._lock:
            history = self._chats.setdefault(chat_id, [])
            history.extend(
                Message(m.role, truncate(m.content, MAX_STORED_MESSAGE_CHARS))
                for m in messages
            )
            self._chats[chat_id] = self._trim(history)

    def reset(self, chat_id: int) -> int:
        """Начинает новый чат. Возвращает, сколько сообщений было забыто."""
        with self._lock:
            return len(self._chats.pop(chat_id, ()))

    def size(self, chat_id: int) -> int:
        with self._lock:
            return len(self._chats.get(chat_id, ()))

    def _trim(self, history: list[Message]) -> list[Message]:
        """Оставляет хвост: свежие реплики важнее старых.

        Обрезка идёт с начала, поэтому первым может остаться результат
        инструмента без своего вызова — модель это переживает, а вот
        переполненный контекст мелкой локальной модели уже нет.
        """
        if len(history) > self._max_messages:
            history = history[-self._max_messages:]

        total = sum(len(m.content) for m in history)
        while len(history) > 1 and total > self._max_chars:
            total -= len(history[0].content)
            history = history[1:]
        return history
