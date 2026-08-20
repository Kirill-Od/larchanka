"""Общая пост-обработка ответов моделей."""

from __future__ import annotations

import re

# Reasoning-модели (qwen3 и другие) пишут ход мысли в <think>...</think>.
# Пользователю это отдавать не нужно, поэтому чистим на уровне ядра —
# любой провайдер получает поведение бесплатно.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    # Если генерацию оборвало по лимиту токенов, закрывающего тега не будет.
    text = _UNCLOSED_THINK.sub("", text)
    return text.strip()
