"""Провайдер локальной модели через Ollama (/api/generate).

Настройки: OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_THREAD.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from bot.core.contracts import LLMError, LLMProvider, Message
from bot.core.text import strip_reasoning, to_api_messages
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

    @property
    def _hint(self) -> str:
        """Самая частая причина отказа — дефолтный localhost в контейнере,
        где Ollama живёт под другим именем."""
        if "localhost" in self._base_url or "127.0.0.1" in self._base_url:
            return (
                "Если бот в контейнере, localhost — это сам контейнер: задай "
                "OLLAMA_URL (docker compose — http://ollama:11434, Railway — "
                "http://ollama.railway.internal:11434)"
            )
        return "Проверь, что Ollama запущена и OLLAMA_URL указывает на неё"

    def _http_error(self, code: int, detail: str) -> str:
        if code == 404:
            return f"модель {self._model!r} не найдена. Скачай её: `ollama pull {self._model}`"
        return ""

    def _payload(self, extra: dict) -> dict:
        payload: dict = {"model": self._model, "stream": False, **extra}
        if self._options:
            payload["options"] = self._options
        return payload

    def generate(self, prompt: str) -> str:
        data = post_json(
            f"{self._base_url}/api/generate",
            self._payload({"prompt": prompt}),
            self._timeout,
            on_http_error=self._http_error,
            unreachable_hint=self._hint,
        )
        return self._answer(data.get("response", ""))

    def chat(self, messages: Sequence[Message]) -> str:
        """Агентный цикл идёт через /api/chat: роли модель понимает лучше,
        чем один склеенный промпт, а Ollama сама применяет шаблон модели."""
        data = post_json(
            f"{self._base_url}/api/chat",
            self._payload({"messages": to_api_messages(messages)}),
            self._timeout,
            on_http_error=self._http_error,
            unreachable_hint=self._hint,
        )
        return self._answer((data.get("message") or {}).get("content", ""))

    @staticmethod
    def _answer(raw: str) -> str:
        answer = strip_reasoning(raw)
        if not answer:
            raise LLMError("модель вернула пустой ответ")
        return answer

    def health(self) -> bool:
        return is_reachable(f"{self._base_url}/api/tags")
