"""Провайдер локальной модели через Ollama (/api/generate).

Настройки: OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_THREAD, OLLAMA_NUM_CTX,
OLLAMA_THINK.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from bot.core.contracts import EmptyAnswer, LLMProvider, Message
from bot.core.text import strip_reasoning, to_api_messages
from bot.providers import register
from bot.providers._http import is_reachable, post_json

#: Ollama по умолчанию берёт 4096 токенов. Нам этого мало: в контексте лежат
#: системный промпт, тело скилла (до 6000 символов) и вывод команд (до 4000
#: на вызов). Переполнение съедает бюджет генерации, и модель возвращает
#: пустой ответ ровно на финальном шаге, когда данные уже собраны.
DEFAULT_NUM_CTX = 8192

#: Целые опции Ollama: имя переменной окружения → имя поля в options.
_INT_OPTIONS = {"OLLAMA_NUM_THREAD": "num_thread", "OLLAMA_NUM_CTX": "num_ctx"}

_TRUE = ("1", "true", "yes", "on", "да")
_FALSE = ("0", "false", "no", "off", "нет")


def _parse_think(raw: str) -> bool | None:
    """None — поля think в запросе не будет вовсе.

    Модели без поддержки размышлений на это поле ругаются, поэтому по
    умолчанию мы его не шлём: включается осознанно, под конкретную модель.
    """
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


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
        self._options: dict[str, int] = {"num_ctx": DEFAULT_NUM_CTX}
        for key, option in _INT_OPTIONS.items():
            raw = settings.get(key, "").strip()
            if not raw:
                continue
            try:
                self._options[option] = int(raw)
            except ValueError:
                pass

        # Размышления reasoning-моделей уходят в отдельное поле ответа, а не
        # в content, и стоят дорого: qwen3:1.7b тратит на «привет» 150 токенов
        # вместо 5. Выключение — самый дешёвый способ ускорить бота на CPU.
        self._think = _parse_think(settings.get("OLLAMA_THINK", ""))

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
        if self._think is not None:
            payload["think"] = self._think
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
            # Не LLMError: у харнесса на это есть отдельный сценарий.
            raise EmptyAnswer("модель вернула пустой ответ")
        return answer

    def health(self) -> bool:
        return is_reachable(f"{self._base_url}/api/tags")
