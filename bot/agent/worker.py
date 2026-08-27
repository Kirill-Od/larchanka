"""Тело рабочего процесса агента.

Процесс живёт отдельно от бота: сеть Telegram, инференс и — что важнее —
выполнение команд полностью изолированы. Падение, зависание или тяжёлая
команда не роняют бота: процесс убивается и поднимается заново.

Провайдер, инструменты и скиллы создаются здесь, внутри процесса, и живут
до его смерти: перечитывать скиллы с диска на каждый запрос незачем.
"""

from __future__ import annotations

import logging
import os
import queue as queue_mod
import signal
import time
from typing import Any

from bot.agent.harness import Harness
from bot.agent.protocol import Claim, Result, Step, Task
from bot.agent.skills import SkillLibrary
from bot.core.contracts import LLMError

logger = logging.getLogger("agent.worker")


def worker_main(
    task_queue: Any,
    result_queue: Any,
    provider_name: str,
    settings: dict[str, str],
    timeout: int,
    log_level: str,
    max_steps: int = 8,
    task_timeout: float = 300.0,
) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s[%(process)d]: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Ctrl+C обрабатывает родитель: воркер завершается по сигнальной задаче None.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from bot import providers, tools  # импорт внутри процесса: реестры строятся заново

    toolset = tools.create_all(settings)
    skills = SkillLibrary.load(settings.get("SKILLS_DIR", ""))
    provider = None
    pid = os.getpid()
    logger.info(
        "Рабочий процесс агента запущен: провайдер %s, инструменты [%s], скиллы [%s]",
        provider_name, ", ".join(toolset) or "—", ", ".join(skills.names()) or "—",
    )

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
            harness = Harness(
                provider=provider,
                tools=toolset,
                skills=skills,
                max_steps=max_steps,
                time_budget=task_timeout,
                on_step=lambda text, tid=task.task_id: result_queue.put(Step(tid, text)),
            )
            run = harness.run(task.messages)
            result_queue.put(
                Result(
                    task.task_id, True,
                    text=run.text,
                    elapsed=time.monotonic() - started,
                    trace=run.trace,
                    steps=run.steps,
                    stopped=run.stopped,
                )
            )
        except LLMError as exc:
            result_queue.put(
                Result(task.task_id, False, error=str(exc), elapsed=time.monotonic() - started)
            )
        except Exception as exc:  # noqa: BLE001 — воркер не имеет права падать молча
            logger.exception("Ошибка в цикле агента")
            result_queue.put(
                Result(
                    task.task_id,
                    False,
                    error=f"внутренняя ошибка агента: {exc.__class__.__name__}",
                    elapsed=time.monotonic() - started,
                )
            )

    logger.info("Рабочий процесс агента остановлен")
