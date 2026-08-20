"""Сообщения между процессом бота и процессами агента.

Только простые dataclass'ы: они уходят через multiprocessing.Queue и обязаны
быть сериализуемыми.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    task_id: str
    prompt: str


@dataclass(frozen=True)
class Claim:
    """Воркер сообщает, что взял задачу. Нужен, чтобы по таймауту знать,
    какой именно процесс убивать."""

    task_id: str
    pid: int


@dataclass(frozen=True)
class Result:
    task_id: str
    ok: bool
    text: str = ""
    error: str = ""
    elapsed: float = 0.0
