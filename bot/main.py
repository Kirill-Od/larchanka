"""Точка входа: собирает приложение из плагинов и запускает транспорт.

Схема работы: Telegram → транспорт (polling | webhook) → MessageHandler →
процесс агента → агентный цикл (модель ⇄ инструменты) → ответ в чат.

Контекст чата живёт в процессе бота и едет с задачей: воркеров может быть
несколько, и любой из них должен уметь взять любую задачу.
"""

from __future__ import annotations

import logging
import signal
import threading

from bot import providers, tools, transports
from bot.agent.pool import AgentPool
from bot.agent.skills import SkillLibrary
from bot.config import Config, ConfigError, load_config
from bot.core.history import ConversationStore
from bot.core.registry import PluginError
from bot.handlers import MessageHandler
from bot.telegram import TelegramClient, TelegramError

logger = logging.getLogger("bot")

_shutdown = threading.Event()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def install_signal_handlers() -> None:
    def handler(signum: int, _frame: object) -> None:
        logger.info("Получен сигнал %s, завершаюсь…", signal.Signals(signum).name)
        _shutdown.set()

    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except ValueError:
        # Обработчики сигналов ставятся только из главного потока.
        # Если бота встроили в чужое приложение — просто работаем без них.
        logger.debug("Сигналы недоступны вне главного потока")


def warn_open_access(config: Config) -> None:
    """Открытый бот с инструментом exec — это открытый шелл на машине.

    До появления агента пустой whitelist означал «отвечаю всем»; теперь он
    означает «выполняю команды по просьбе кого угодно». Падать не падаем —
    решение за владельцем, — но молчать об этом нельзя.
    """
    if config.allowed_user_ids:
        return
    enabled = tools.available(config.settings.get("TOOL_PACKAGES", ""))
    if "exec" not in enabled or "exec" in config.settings.get("TOOLS_DISABLED", ""):
        return
    logger.warning(
        "ALLOWED_USER_IDS пуст, а инструмент exec включён: команды в системе "
        "сможет выполнять любой, кто найдёт бота. Впиши свой user_id "
        "в ALLOWED_USER_IDS или отключи инструмент через TOOLS_DISABLED=exec."
    )


def probe_provider(config: Config) -> str:
    """Проверяет провайдер в процессе бота и возвращает имя модели для /start.

    Ждём с ретраями: в Docker Compose бот может стартовать раньше Ollama,
    и падать из-за этого он не должен.
    """
    provider = providers.create(
        config.llm_provider, config.provider_settings, config.llm_timeout
    )
    for attempt in range(1, 11):
        if provider.health():
            logger.info(
                "Провайдер %s готов, модель %s", config.llm_provider, provider.model
            )
            return provider.model
        logger.warning(
            "Провайдер %s ещё не отвечает, попытка %d/10", config.llm_provider, attempt
        )
        if _shutdown.wait(3.0):
            break
    logger.warning(
        "Провайдер %s недоступен на старте — продолжаю работу, "
        "но до его запуска пользователи будут получать ошибку.",
        config.llm_provider,
    )
    return provider.model


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging("INFO")
        logger.error("Ошибка конфигурации: %s", exc)
        return 1

    setup_logging(config.log_level)
    install_signal_handlers()

    logger.info(
        "Плагины: инференс %s (доступны: %s), транспорт %s (доступны: %s)",
        config.llm_provider,
        ", ".join(providers.available(config.settings.get("PLUGIN_PACKAGES", ""))),
        config.transport, ", ".join(transports.available()),
    )
    skills = SkillLibrary.load(config.settings.get("SKILLS_DIR", ""))
    logger.info(
        "Агент: инструменты [%s], скиллы [%s], потолок %d шагов, бюджет %d с",
        ", ".join(tools.available(config.settings.get("TOOL_PACKAGES", ""))) or "—",
        ", ".join(skills.names()) or "—",
        config.agent_max_steps, config.agent_task_timeout,
    )
    if not skills.names():
        logger.warning(
            "Скиллов не найдено в %s — агент будет работать одними инструментами",
            skills.directory,
        )
    warn_open_access(config)

    telegram = TelegramClient(config.telegram_bot_token)
    try:
        me = telegram.get_me()
    except TelegramError as exc:
        logger.error(
            "Не удалось подключиться к Telegram (token %s): %s", config.token_hint, exc
        )
        return 1
    logger.info("Бот @%s запущен", me.get("username", "unknown"))

    try:
        model_name = probe_provider(config)
        transport = transports.create(
            config.transport, telegram, config.settings, config.poll_timeout
        )
    except PluginError as exc:
        logger.error("Ошибка плагина: %s", exc)
        return 1

    agent = AgentPool(
        provider_name=config.llm_provider,
        settings=config.provider_settings,
        timeout=config.llm_timeout,
        workers=config.agent_workers,
        log_level=config.log_level,
        max_steps=config.agent_max_steps,
        task_timeout=config.agent_task_timeout,
    )
    agent.start()
    conversations = ConversationStore(
        max_messages=config.history_max_messages,
        max_chars=config.history_max_chars,
    )
    handler = MessageHandler(config, telegram, agent, conversations, model_name)

    try:
        transport.run(handler, _shutdown)
    except ValueError as exc:
        logger.error("Транспорт %s не сконфигурирован: %s", config.transport, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
    finally:
        _shutdown.set()
        agent.shutdown()

    logger.info("Бот остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
