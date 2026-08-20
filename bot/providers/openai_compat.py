"""Провайдер для любого OpenAI-совместимого API: vLLM, llama.cpp server, LM Studio.

Настройки: LLM_BASE_URL (например http://localhost:8000/v1), LLM_MODEL,
LLM_API_KEY (если сервер его требует).

Плагин добавлен, чтобы показать расширяемость: подключается сменой
LLM_PROVIDER=openai_compat, ни одна другая строка кода не меняется.
"""

from __future__ import annotations

from collections.abc import Mapping

from bot.core.contracts import LLMError, LLMProvider
from bot.core.text import strip_reasoning
from bot.providers import register
from bot.providers._http import is_reachable, post_json


@register("openai_compat")
class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, settings: Mapping[str, str], timeout: int):
        super().__init__(settings, timeout)
        self._base_url = settings.get("LLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
        self._model = settings.get("LLM_MODEL", "qwen3:1.7b")
        self._api_key = settings.get("LLM_API_KEY", "")

    @property
    def model(self) -> str:
        return self._model

    def generate(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = post_json(
            f"{self._base_url}/chat/completions",
            {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            self._timeout,
            headers=headers,
        )
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("сервер не вернул ни одного варианта ответа")
        answer = strip_reasoning(choices[0].get("message", {}).get("content", ""))
        if not answer:
            raise LLMError("модель вернула пустой ответ")
        return answer

    def health(self) -> bool:
        return is_reachable(f"{self._base_url}/models")
