"""Общий HTTP-хелпер для провайдеров. Только стандартная библиотека."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from bot.core.contracts import LLMError


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: int,
    headers: dict[str, str] | None = None,
    on_http_error: Any = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("error", "") if isinstance(body, dict) else ""
            if isinstance(detail, dict):
                detail = detail.get("message", "")
        except (ValueError, OSError):
            pass
        if on_http_error is not None:
            message = on_http_error(exc.code, detail)
            if message:
                raise LLMError(message) from None
        raise LLMError(f"HTTP {exc.code}: {detail or exc.reason}") from None
    except (socket.timeout, TimeoutError):
        raise LLMError(f"модель не ответила за {timeout} с") from None
    except urllib.error.URLError as exc:
        raise LLMError(f"сервис недоступен по адресу {url} ({exc.reason})") from None
    except json.JSONDecodeError as exc:
        raise LLMError(f"некорректный JSON в ответе ({exc})") from None


def is_reachable(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        return False
