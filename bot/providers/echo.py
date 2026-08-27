"""Провайдер-заглушка: возвращает текст пользователя.

Нужен, чтобы проверить всю цепочку Telegram → агент → ответ без установки
модели (LLM_PROVIDER=echo). Заодно это пример минимального плагина:
метод chat() он не реализует, и агентный цикл всё равно с ним работает —
базовая реализация сама склеит контекст в один промпт.
"""

from __future__ import annotations

from bot.core.contracts import LLMProvider
from bot.providers import register


@register("echo")
class EchoProvider(LLMProvider):
    name = "echo"

    @property
    def model(self) -> str:
        return "echo (заглушка без модели)"

    def generate(self, prompt: str) -> str:
        return f"Эхо: {prompt}"
