"""Бизнес-логика бота: апдейт → агент → ответ.

Модуль не знает, каким транспортом пришёл апдейт (polling или webhook),
и не знает, какой провайдер считает ответ. Оба слоя подключаются снаружи.

Здесь же живёт контекст чата: чат — одна длинная беседа, /new начинает новую.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from bot.agent.pool import AgentError, AgentPool
from bot.agent.skills import SkillLibrary
from bot.config import Config
from bot.core.contracts import Message
from bot.core.history import ConversationStore
from bot.telegram import TelegramClient

logger = logging.getLogger("bot.handlers")

# Длинный «роман» в промпте только замедлит локальную модель.
MAX_PROMPT_CHARS = 4000

HELP_TEXT = (
    "Я агент: у меня есть инструменты, и на твой запрос я могу выполнять "
    "реальные действия — команды в терминале, запросы к API через curl, "
    "чтение файлов.\n\n"
    "Чат — одна длинная беседа: я помню, о чём мы говорили выше. "
    "Начать с чистого листа — /new.\n\n"
    "Команды:\n"
    "/new — новый чат, забыть контекст\n"
    "/skills — какие инструкции я знаю\n"
    "/help — эта справка"
)


class TypingIndicator:
    """Держит статус «печатает», пока агент считает.

    Telegram гасит индикатор через ~5 с, а локальная модель на CPU отвечает
    дольше, поэтому статус приходится продлевать в фоне.
    """

    def __init__(self, telegram: TelegramClient, chat_id: int, interval: float = 4.0):
        self._telegram = telegram
        self._chat_id = chat_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._telegram.send_chat_action(self._chat_id)
            self._stop.wait(self._interval)

    def __enter__(self) -> "TypingIndicator":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class MessageHandler:
    def __init__(
        self,
        config: Config,
        telegram: TelegramClient,
        agent: AgentPool,
        conversations: ConversationStore,
        model_name: str = "локальная модель",
    ):
        self._config = config
        self._telegram = telegram
        self._agent = agent
        self._conversations = conversations
        self._model_name = model_name
        # Каталог скиллов для /skills. Сам агент читает их в своём процессе.
        self._skills = SkillLibrary.load(config.settings.get("SKILLS_DIR", ""))

    def __call__(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if message:
            self.handle_message(message)

    def _is_allowed(self, user_id: int) -> bool:
        allowed = self._config.allowed_user_ids
        return not allowed or user_id in allowed

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message.get("chat", {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if chat_id is None or user_id is None:
            return

        if not self._is_allowed(user_id):
            logger.warning("Отклонён запрос от user_id=%s (не в whitelist)", user_id)
            self._telegram.send_message(chat_id, "Извини, у меня закрытый доступ.")
            return

        text = (message.get("text") or "").strip()
        if not text:
            self._telegram.send_message(chat_id, "Я понимаю только текстовые сообщения.")
            return

        if text.startswith("/"):
            self._handle_command(chat_id, text)
            return

        self._run_agent(chat_id, user_id, text)

    def _handle_command(self, chat_id: int, text: str) -> None:
        command = text.split(maxsplit=1)[0].split("@")[0].lower()
        if command == "/start":
            self._conversations.reset(chat_id)
            self._telegram.send_message(
                chat_id,
                f"Привет! Я агент на модели {self._model_name}.\n\n" + HELP_TEXT,
            )
        elif command == "/help":
            self._telegram.send_message(chat_id, HELP_TEXT)
        elif command == "/new":
            forgotten = self._conversations.reset(chat_id)
            self._telegram.send_message(
                chat_id,
                f"Новый чат. Забыл предыдущий контекст ({forgotten} сообщ.)."
                if forgotten
                else "Новый чат. Контекст и так был пуст.",
            )
        elif command == "/skills":
            self._telegram.send_message(chat_id, self._skills_text())
        else:
            self._telegram.send_message(chat_id, "Не знаю такой команды. Напиши /help.")

    def _skills_text(self) -> str:
        if not len(self._skills):
            return (
                "Скиллов нет. Положи .md-файл с инструкцией в каталог "
                f"{self._skills.directory} — я подхвачу его при следующем запуске."
            )
        lines = [f"• {s.name} — {s.description}" for s in self._skills]
        return (
            "Инструкции, которые я знаю (читаю целиком, когда задача похожа):\n\n"
            + "\n".join(lines)
        )

    def _run_agent(self, chat_id: int, user_id: int, text: str) -> None:
        prompt = text[:MAX_PROMPT_CHARS]
        if len(text) > MAX_PROMPT_CHARS:
            logger.info(
                "Сообщение user_id=%s обрезано до %d символов", user_id, MAX_PROMPT_CHARS
            )

        history = self._conversations.history(chat_id)
        question = Message("user", prompt)
        logger.info(
            "Запрос от user_id=%s, длина %d символов, контекст %d сообщ.",
            user_id, len(prompt), len(history),
        )

        started = time.monotonic()
        try:
            with TypingIndicator(self._telegram, chat_id):
                answer = self._agent.run(
                    [*history, question], on_step=self._step_reporter(chat_id)
                )
        except AgentError as exc:
            logger.error("Агент не справился (user_id=%s): %s", user_id, exc)
            self._telegram.send_message(chat_id, f"Не смог выполнить задачу: {exc}")
            # Вопрос всё равно кладём в контекст: следующий запрос вида
            # «попробуй ещё раз» должен понимать, о чём речь.
            self._conversations.extend(chat_id, [question])
            return

        self._conversations.extend(chat_id, [question, *answer.trace])
        elapsed = time.monotonic() - started
        logger.info(
            "Ответ для user_id=%s за %.1f с: шагов %d (%s), %d символов",
            user_id, elapsed, answer.steps, answer.stopped, len(answer.text),
        )
        self._telegram.send_message(chat_id, answer.text)

    def _step_reporter(self, chat_id: int):
        """Показывает в чате, что агент делает прямо сейчас.

        Полезно вдвойне: пользователь видит прогресс на медленной модели
        и видит, какие именно команды выполняются от его имени.
        """
        if not self._config.agent_show_steps:
            return None

        def report(text: str) -> None:
            try:
                self._telegram.send_message(chat_id, f"⚙️ {text}")
            except Exception:  # noqa: BLE001 — прогресс не важнее ответа
                logger.debug("Не удалось отправить шаг в чат", exc_info=True)

        return report
