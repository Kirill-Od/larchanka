"""Транспорт webhook — вариант «Python Webhook» со схемы.

Telegram сам присылает апдейты POST-запросом на наш публичный HTTPS-адрес.
Сервер — из стандартной библиотеки, TLS терминируется обратным прокси
(nginx / Caddy), как это обычно и делается.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot.core.contracts import Transport, UpdateHandler
from bot.telegram import TelegramClient, TelegramError
from bot.transports import register

logger = logging.getLogger("transport.webhook")

MAX_BODY_BYTES = 1024 * 1024


@register("webhook")
class WebhookTransport(Transport):
    name = "webhook"

    def __init__(self, telegram: TelegramClient, settings, poll_timeout: int = 30):
        self._telegram = telegram
        self._public_url = settings.get("WEBHOOK_URL", "").strip().rstrip("/")
        self._listen = settings.get("WEBHOOK_LISTEN", "0.0.0.0").strip()
        self._port = int(settings.get("WEBHOOK_PORT", "8080") or 8080)
        self._path = "/" + settings.get("WEBHOOK_PATH", "telegram").strip().lstrip("/")
        self._secret = settings.get("WEBHOOK_SECRET_TOKEN", "").strip()
        self._workers = int(settings.get("WEBHOOK_WORKERS", "4") or 4)

    def run(self, handler: UpdateHandler, stop: threading.Event) -> None:
        if not self._public_url:
            raise ValueError(
                "WEBHOOK_URL не задан. Для транспорта webhook нужен публичный "
                "HTTPS-адрес, например https://bot.example.com"
            )
        if not self._public_url.startswith("https://"):
            raise ValueError("Telegram принимает webhook только по HTTPS")

        transport = self
        pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="webhook")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:
                logger.debug("%s - %s", self.address_string(), fmt % args)

            def _reply(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
                if self.path != transport._path:
                    return self._reply(404)
                if transport._secret and (
                    self.headers.get("X-Telegram-Bot-Api-Secret-Token") != transport._secret
                ):
                    logger.warning("Отклонён запрос с неверным secret token")
                    return self._reply(403)

                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY_BYTES:
                    return self._reply(400)
                try:
                    update = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return self._reply(400)

                # Отвечаем сразу: Telegram ждёт быстрый 200, иначе шлёт повторы,
                # а инференс занимает секунды. Обработка уходит в пул потоков,
                # сам вызов LLM всё равно происходит в отдельном процессе.
                self._reply(200)
                pool.submit(transport._safe_handle, handler, update)

        server = ThreadingHTTPServer((self._listen, self._port), Handler)
        server.daemon_threads = True

        webhook_url = f"{self._public_url}{self._path}"
        self._telegram.set_webhook(webhook_url, secret_token=self._secret)
        logger.info("Webhook установлен: %s (слушаю %s:%d)", webhook_url, self._listen, self._port)
        if not self._secret:
            logger.warning(
                "WEBHOOK_SECRET_TOKEN пуст: эндпоинт примет апдейт от кого угодно, "
                "кто знает адрес. Задай секрет."
            )

        stopper = threading.Thread(
            target=lambda: (stop.wait(), server.shutdown()), daemon=True
        )
        stopper.start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            pool.shutdown(wait=False, cancel_futures=True)
            try:
                self._telegram.delete_webhook()
                logger.info("Webhook снят")
            except TelegramError as exc:
                logger.warning("Не удалось снять webhook: %s", exc)

    @staticmethod
    def _safe_handle(handler: UpdateHandler, update: dict) -> None:
        try:
            handler(update)
        except Exception:
            logger.exception("Ошибка обработки апдейта")
