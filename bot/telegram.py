"""Клиент Telegram Bot API на чистом HTTP (urllib), без сторонних библиотек.

Так мы понимаем устройство Bot API изнутри и не тянем в проект зависимости,
через которые возможна supply chain атака.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Жёсткий лимит Telegram на длину текстового сообщения.
MAX_MESSAGE_LENGTH = 4096


class TelegramError(Exception):
    """Базовая ошибка работы с Bot API."""


class TelegramNetworkError(TelegramError):
    """Сеть недоступна или истёк таймаут — имеет смысл повторить."""


class TelegramAPIError(TelegramError):
    """Bot API вернул ok=false."""

    def __init__(self, status: int, description: str, retry_after: int | None = None):
        super().__init__(f"HTTP {status}: {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Режет длинный ответ на части, по возможности по границе строки."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        window = text[:limit]
        # Ищем ближайший разумный перенос, чтобы не рвать слово посередине.
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [p for p in parts if p]


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30):
        self._token = token
        self._timeout = timeout

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    def _request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self._url(method),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._api_error(exc) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise TelegramNetworkError(f"{method}: сеть недоступна ({exc})") from None
        except json.JSONDecodeError as exc:
            raise TelegramError(f"{method}: некорректный JSON в ответе ({exc})") from None

        if not data.get("ok"):
            raise TelegramAPIError(200, data.get("description", "ok=false"))
        return data.get("result")

    @staticmethod
    def _api_error(exc: urllib.error.HTTPError) -> TelegramAPIError:
        """Разбирает тело ошибки: там лежит description и retry_after для 429."""
        description = exc.reason or "unknown error"
        retry_after: int | None = None
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            description = payload.get("description", description)
            retry_after = payload.get("parameters", {}).get("retry_after")
        except (ValueError, OSError):
            pass
        return TelegramAPIError(exc.code, description, retry_after)

    def get_me(self) -> dict[str, Any]:
        return self._request("getMe")

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        """Webhook и long polling несовместимы: getUpdates вернёт 409 Conflict."""
        self._request("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    def set_webhook(
        self,
        url: str,
        secret_token: str = "",
        allowed_updates: list[str] | None = None,
        drop_pending_updates: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "url": url,
            "allowed_updates": allowed_updates or ["message"],
            "drop_pending_updates": drop_pending_updates,
        }
        if secret_token:
            # Telegram будет слать его в X-Telegram-Bot-Api-Secret-Token,
            # чтобы наш эндпоинт не принимал апдейты от посторонних.
            payload["secret_token"] = secret_token
        self._request("setWebhook", payload)

    def get_webhook_info(self) -> dict[str, Any]:
        return self._request("getWebhookInfo")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        # HTTP-таймаут должен быть заведомо больше long polling, иначе клиент
        # оборвёт соединение раньше, чем сервер успеет ответить пустым списком.
        return self._request("getUpdates", payload, timeout=timeout + 15) or []

    def send_message(self, chat_id: int, text: str) -> None:
        for part in split_text(text):
            self._request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": part,
                    "disable_web_page_preview": True,
                },
            )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Индикатор «печатает». Ошибка здесь не должна ломать обработку."""
        try:
            self._request("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError as exc:
            logger.debug("sendChatAction не удался: %s", exc)
