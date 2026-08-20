"""Провайдер локальной модели через Ollama (/api/generate).

Настройки: OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_THREAD.
"""

from __future__ import annotations

from collections.abc import Mapping

from bot.core.contracts import LLMError, LLMProvider
from bot.core.text import strip_reasoning
from bot.providers import register
from bot.providers._http import is_reachable, post_json


@register("ollama")
class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, settings: Mapping[str, str], timeout: int):
        super().__init__(settings, timeout)
        self._base_url = settings.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        self._model = settings.get("OLLAMA_MODEL", "qwen3:1.7b")

        # В контейнере Ollama видит все ядра хоста и запускает поток на каждое,
        # хотя cgroup выделяет заметно меньше. Потоки начинают душить друг друга:
        # на Railway (48 ядер хоста, квота 24) это разница в 25 раз — 0.8 ток/с
        # против 20. Поэтому число потоков задаётся явно.
        self._options: dict[str, int] = {}
        threads = settings.get("OLLAMA_NUM_THREAD", "").strip()
        if threads:
            try:
                self._options["num_thread"] = int(threads)
            except ValueError:
                pass

    @property
    def model(self) -> str:
        return self._model

    def _http_error(self, code: int, detail: str) -> str:
        if code == 404:
            return f"модель {self._model!r} не найдена. Скачай её: `ollama pull {self._model}`"
        return ""

    def generate(self, prompt: str) -> str:
        payload: dict = {"model": self._model, "prompt": prompt, "stream": False}
        if self._options:
            payload["options"] = self._options
        data = post_json(
            f"{self._base_url}/api/generate",
            payload,
            self._timeout,
            on_http_error=self._http_error,
        )
        answer = strip_reasoning(data.get("response", ""))
        if not answer:
            raise LLMError("модель вернула пустой ответ")
        return answer

    def health(self) -> bool:
        return is_reachable(f"{self._base_url}/api/tags")
