"""Тело рабочего процесса агента.

Процесс живёт отдельно от бота: сеть Telegram и инференс полностью изолированы.
Падение или зависание модели не роняет бота — процесс просто убивается
и поднимается заново.
"""

from __future__ import annotations

import logging
import os
import queue as queue_mod
import signal
import time
from typing import Any

from bot.core.contracts import LLMError
from bot.agent.protocol import Claim, Result, Task

logger = logging.getLogger("agent.worker")


def worker_main(
    task_queue: Any,
    result_queue: Any,
    provider_name: str,
    settings: dict[str, str],
    timeout: int,
    log_level: str,
) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s[%(process)d]: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Ctrl+C обрабатывает родитель: воркер завершается по сигнальной задаче None.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from bot import providers  # импорт внутри процесса: реестр строится заново

    provider = None
    pid = os.getpid()
    logger.info("Рабочий процесс агента запущен (провайдер %s)", provider_name)

    while True:
        try:
            task = task_queue.get(timeout=0.5)
        except queue_mod.Empty:
            continue
        except (EOFError, OSError):
            break

        if task is None:  # сигнал остановки
            break
        if not isinstance(task, Task):
            continue

        result_queue.put(Claim(task.task_id, pid))
        started = time.monotonic()
        try:
            if provider is None:
                # Ленивое создание: ошибка конфигурации станет ответом на задачу,
                # а не молчаливой смертью процесса на старте.
                provider = providers.create(provider_name, settings, timeout)
            text = provider.generate(task.prompt)
            result_queue.put(
                Result(task.task_id, True, text=text, elapsed=time.monotonic() - started)
            )
        except LLMError as exc:
            result_queue.put(
                Result(task.task_id, False, error=str(exc), elapsed=time.monotonic() - started)
            )
        except Exception as exc:  # noqa: BLE001 — воркер не имеет права падать молча
            logger.exception("Ошибка инференса")
            result_queue.put(
                Result(
                    task.task_id,
                    False,
                    error=f"внутренняя ошибка агента: {exc.__class__.__name__}",
                    elapsed=time.monotonic() - started,
                )
            )

    logger.info("Рабочий процесс агента остановлен")
