"""Сообщения между процессом бота и процессами агента.

Только простые dataclass'ы: они уходят через multiprocessing.Queue и обязаны
быть сериализуемыми.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.core.contracts import Message


@dataclass(frozen=True)
class Task:
    task_id: str
    #: Весь контекст чата: система его не хранит в воркере, потому что задачу
    #: может взять любой из процессов пула.
    messages: tuple[Message, ...]


@dataclass(frozen=True)
class Claim:
    """Воркер сообщает, что взял задачу. Нужен, чтобы по таймауту знать,
    какой именно процесс убивать."""

    task_id: str
    pid: int


@dataclass(frozen=True)
class Step:
    """Промежуточный шаг цикла — чтобы пользователь видел, что агент делает,
    а не смотрел на «печатает…» минуту."""

    task_id: str
    text: str


@dataclass(frozen=True)
class Result:
    task_id: str
    ok: bool
    text: str = ""
    error: str = ""
    elapsed: float = 0.0
    #: Что цикл добавил в контекст — вызовы, результаты, финальный ответ.
    trace: tuple[Message, ...] = ()
    steps: int = 0
    stopped: str = ""
