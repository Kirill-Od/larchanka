"""Транспорт long polling — вариант «Python local» со схемы.

Не требует домена, TLS и белого IP: бот сам ходит за апдейтами.
"""

from __future__ import annotations

import logging
import threading

from bot.core.contracts import Transport, UpdateHandler
from bot.telegram import TelegramAPIError, TelegramClient, TelegramNetworkError
from bot.transports import register

logger = logging.getLogger("transport.polling")

MAX_BACKOFF_SECONDS = 60


@register("polling")
class PollingTransport(Transport):
    name = "polling"

    def __init__(self, telegram: TelegramClient, settings, poll_timeout: int = 30):
        self._telegram = telegram
        self._poll_timeout = poll_timeout

    def _drop_pending_updates(self) -> int | None:
        """Пропускает очередь, накопленную пока бот был офлайн.

        offset=-1 отдаёт только последний апдейт; следующий offset за ним
        подтверждает и удаляет всё, что пришло раньше.
        """
        updates = self._telegram.get_updates(offset=-1, timeout=0)
        if not updates:
            return None
        logger.info("Пропущены сообщения, накопившиеся пока бот был офлайн")
        return updates[-1]["update_id"] + 1

    def run(self, handler: UpdateHandler, stop: threading.Event) -> None:
        # Webhook и polling взаимоисключающи, иначе getUpdates отдаст 409.
        self._telegram.delete_webhook()
        offset = self._drop_pending_updates()
        failures = 0
        logger.info("Long polling запущен (timeout %d с)", self._poll_timeout)

        while not stop.is_set():
            try:
                updates = self._telegram.get_updates(offset, timeout=self._poll_timeout)
                failures = 0
            except TelegramAPIError as exc:
                if exc.status == 409:
                    logger.error(
                        "Конфликт getUpdates: с этим токеном уже работает другой "
                        "экземпляр бота или установлен webhook. Останови вторую копию."
                    )
                wait = exc.retry_after or min(2 ** failures, MAX_BACKOFF_SECONDS)
                logger.error("Ошибка Bot API: %s. Пауза %s с", exc, wait)
                failures += 1
                stop.wait(wait)
                continue
            except TelegramNetworkError as exc:
                wait = min(2 ** failures, MAX_BACKOFF_SECONDS)
                logger.warning("Сеть недоступна: %s. Повтор через %s с", exc, wait)
                failures += 1
                stop.wait(wait)
                continue

            for update in updates:
                # Сдвигаем offset до обработки: иначе при ошибке бот
                # зациклится на одном и том же сообщении.
                offset = update["update_id"] + 1
                if stop.is_set():
                    break
                handler(update)
