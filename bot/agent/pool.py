"""Родительская сторона агента: пул рабочих процессов и синхронный вызов.

Зачем отдельный процесс, а не поток:
  * жёсткий таймаут — зависший инференс можно убить, поток убить нельзя;
  * изоляция — падение провайдера не роняет процесс бота;
  * GIL не мешает: транспорт продолжает принимать апдейты, пока агент считает.
"""

from __future__ import annotations

import logging
import multiprocessing
import queue as queue_mod
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from bot.agent.protocol import Claim, Result, Step, Task
from bot.agent.worker import worker_main
from bot.core.contracts import Message

logger = logging.getLogger("agent.pool")

# Запас поверх бюджета задачи: сначала должен сработать дедлайн харнесса
# с понятным текстом, и только зависший наглухо процесс убиваем силой.
KILL_MARGIN_SECONDS = 15


class AgentError(Exception):
    """Агент не смог выполнить задачу."""


class AgentTimeout(AgentError):
    """Процесс агента не ответил и был снят принудительно."""


@dataclass
class _Slot:
    event: threading.Event
    result: Result | None = None
    #: Куда лить промежуточные шаги цикла (в чат — «⚙️ exec: …»).
    on_step: Callable[[str], None] | None = None


@dataclass(frozen=True)
class AgentAnswer:
    """Ответ агента вместе с тем, что он добавил в контекст чата."""

    text: str
    trace: tuple[Message, ...] = ()
    steps: int = 0
    stopped: str = "answer"


class AgentPool:
    def __init__(
        self,
        provider_name: str,
        settings: Mapping[str, str],
        timeout: int,
        workers: int = 1,
        log_level: str = "INFO",
        max_steps: int = 8,
        task_timeout: int = 300,
    ):
        self._provider_name = provider_name
        self._settings = dict(settings)
        self._timeout = timeout
        self._max_steps = max_steps
        # Один запрос — это несколько вызовов модели плюс работа инструментов,
        # поэтому ждём бюджет задачи, а не таймаут одного вызова.
        self._task_timeout = task_timeout
        self._workers_count = max(1, workers)
        self._log_level = log_level

        # spawn, а не fork: безопасно рядом с потоками и одинаково на macOS/Linux
        self._ctx = multiprocessing.get_context("spawn")
        self._task_queue = self._ctx.Queue()
        self._result_queue = self._ctx.Queue()

        self._processes: list[multiprocessing.process.BaseProcess] = []
        self._slots: dict[str, _Slot] = {}
        self._claims: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._dispatcher: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None

    # --- жизненный цикл ---------------------------------------------------

    def start(self) -> None:
        for _ in range(self._workers_count):
            self._spawn_worker()
        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher.start()
        self._supervisor = threading.Thread(target=self._supervise_loop, daemon=True)
        self._supervisor.start()
        logger.info(
            "Агент запущен: %d процесс(ов), провайдер %s",
            self._workers_count, self._provider_name,
        )

    def _spawn_worker(self) -> None:
        process = self._ctx.Process(
            target=worker_main,
            args=(
                self._task_queue,
                self._result_queue,
                self._provider_name,
                self._settings,
                self._timeout,
                self._log_level,
                self._max_steps,
                float(self._task_timeout),
            ),
            daemon=True,
        )
        process.start()
        self._processes.append(process)

    def shutdown(self) -> None:
        self._stopping.set()
        for _ in self._processes:
            try:
                self._task_queue.put(None)
            except (ValueError, OSError):
                break
        for process in self._processes:
            process.join(timeout=5)
            if process.is_alive():
                logger.warning("Процесс агента %s не завершился, снимаю", process.pid)
                process.terminate()
        self._processes.clear()
        logger.info("Агент остановлен")

    # --- фоновые потоки ---------------------------------------------------

    def _dispatch_loop(self) -> None:
        """Разносит ответы воркеров по ожидающим вызовам."""
        while not self._stopping.is_set():
            try:
                message = self._result_queue.get(timeout=0.5)
            except queue_mod.Empty:
                continue
            except (EOFError, OSError):
                break

            if isinstance(message, Claim):
                with self._lock:
                    self._claims[message.task_id] = message.pid
                continue
            if isinstance(message, Step):
                with self._lock:
                    slot = self._slots.get(message.task_id)
                callback = slot.on_step if slot else None
                if callback is not None:
                    try:
                        callback(message.text)
                    except Exception:  # noqa: BLE001 — UI не должен ломать цикл
                        logger.exception("Обработчик шага упал")
                continue
            if not isinstance(message, Result):
                continue
            with self._lock:
                slot = self._slots.get(message.task_id)
                self._claims.pop(message.task_id, None)
                if slot is None:
                    continue  # вызывающая сторона уже ушла по таймауту
                slot.result = message
            slot.event.set()

    def _supervise_loop(self) -> None:
        """Поднимает процессы, которые умерли или были сняты по таймауту."""
        while not self._stopping.wait(2.0):
            alive = []
            for process in self._processes:
                if process.is_alive():
                    alive.append(process)
                else:
                    logger.warning(
                        "Процесс агента %s умер (код %s), поднимаю заново",
                        process.pid, process.exitcode,
                    )
            self._processes = alive
            while len(self._processes) < self._workers_count and not self._stopping.is_set():
                self._spawn_worker()

    # --- вызов ------------------------------------------------------------

    def run(
        self,
        messages: Sequence[Message],
        on_step: Callable[[str], None] | None = None,
    ) -> AgentAnswer:
        """Синхронный вызов агента с контекстом чата. Потокобезопасен:
        webhook-транспорт обрабатывает апдейты в нескольких потоках."""
        if self._stopping.is_set():
            raise AgentError("агент остановлен")

        task = Task(uuid.uuid4().hex, tuple(messages))
        slot = _Slot(threading.Event(), on_step=on_step)
        with self._lock:
            self._slots[task.task_id] = slot

        try:
            self._task_queue.put(task)
            if not slot.event.wait(self._task_timeout + KILL_MARGIN_SECONDS):
                self._kill_task_owner(task.task_id)
                raise AgentTimeout(
                    f"процесс агента завис и был перезапущен "
                    f"(лимит {self._task_timeout} с)"
                )
            result = slot.result
        finally:
            with self._lock:
                self._slots.pop(task.task_id, None)

        if result is None or not result.ok:
            raise AgentError(result.error if result else "агент не вернул ответ")
        logger.debug(
            "Задача %s выполнена за %.1f с, шагов %d (%s)",
            task.task_id[:8], result.elapsed, result.steps, result.stopped,
        )
        return AgentAnswer(result.text, result.trace, result.steps, result.stopped)

    def _kill_task_owner(self, task_id: str) -> None:
        with self._lock:
            pid = self._claims.pop(task_id, None)
        if pid is None:
            logger.error("Задача %s не была взята ни одним процессом", task_id[:8])
            return
        for process in self._processes:
            if process.pid == pid and process.is_alive():
                logger.error("Снимаю зависший процесс агента %s", pid)
                process.terminate()  # supervisor поднимет замену
                return
