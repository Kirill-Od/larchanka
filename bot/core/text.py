"""Общая пост-обработка ответов моделей и подготовка контекста."""

from __future__ import annotations

import re
from collections.abc import Sequence

from bot.core.contracts import Message

# Reasoning-модели (qwen3 и другие) пишут ход мысли в <think>...</think>.
# Пользователю это отдавать не нужно, поэтому чистим на уровне ядра —
# любой провайдер получает поведение бесплатно.
# Ollama с версии 0.32 сама выносит размышления в отдельное поле ответа,
# так что на этом пути тегов уже нет. Функция всё равно нужна: OpenAI-
# совместимые серверы отдают их прямо в content, как и старые Ollama.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)

_ROLE_LABELS = {
    "system": "Система",
    "user": "Пользователь",
    "assistant": "Ассистент",
    "tool": "Результат инструмента",
}


def strip_reasoning(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    # Если генерацию оборвало по лимиту токенов, закрывающего тега не будет.
    text = _UNCLOSED_THINK.sub("", text)
    return text.strip()


def truncate(text: str, limit: int, note: str = "обрезано") -> str:
    """Режет длинный текст, честно сообщая, сколько символов потеряно."""
    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n… [{note}: ещё {dropped} символов]"


def render_dialog(messages: Sequence[Message]) -> str:
    """Склеивает диалог в один промпт для провайдеров без chat-эндпоинта."""
    parts = [
        f"### {_ROLE_LABELS.get(message.role, message.role)}\n{message.content}"
        for message in messages
    ]
    # Обрываем на пустой реплике ассистента: модель продолжает именно её.
    parts.append(f"### {_ROLE_LABELS['assistant']}\n")
    return "\n\n".join(parts)


def to_api_messages(messages: Sequence[Message]) -> list[dict[str, str]]:
    """Превращает контекст в формат chat-эндпоинтов (Ollama, OpenAI).

    Роль tool отдаём как user с явной пометкой: шаблоны многих моделей
    рендерят настоящую роль tool только вместе с tool_calls, а нам нужен
    один и тот же протокол на любой модели, включая мелкие локальные.
    """
    result: list[dict[str, str]] = []
    for message in messages:
        if message.role == "tool":
            result.append(
                {
                    "role": "user",
                    "content": f"{_ROLE_LABELS['tool']}:\n{message.content}",
                }
            )
        else:
            result.append({"role": message.role, "content": message.content})
    return result
