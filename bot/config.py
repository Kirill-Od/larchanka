"""Конфигурация из .env и переменных окружения.

Приоритет: переменные окружения > .env-файл. Это нужно для Docker Compose,
где OLLAMA_URL задаётся в environment и должен перекрывать значение из env_file.
Токен никогда не логируется и не попадает в repr.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(Exception):
    """Конфигурация отсутствует или некорректна."""


def _parse_env_file(path: Path) -> dict[str, str]:
    """Минимальный парсер .env: KEY=VALUE, # — комментарий, кавычки снимаются."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            raise ConfigError(
                f"ALLOWED_USER_IDS: {chunk!r} не является числовым user_id"
            ) from None
    return frozenset(ids)


def _parse_positive_int(raw: str, name: str, default: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}: ожидалось целое число, получено {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name}: должно быть больше нуля, получено {value}")
    return value


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str = field(repr=False)  # не показываем секрет в repr
    transport: str
    llm_provider: str
    llm_timeout: int
    poll_timeout: int
    agent_workers: int
    allowed_user_ids: frozenset[int]
    log_level: str
    #: Все значения .env + окружения целиком. Плагины читают отсюда свои ключи,
    #: поэтому новый провайдер не требует правок этого файла.
    settings: Mapping[str, str] = field(repr=False)

    #: Ключи, которые не должны покидать процесс бота.
    SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET_TOKEN")

    @property
    def token_hint(self) -> str:
        """Безопасный для логов фрагмент токена: только bot_id до двоеточия."""
        bot_id, _, _ = self.telegram_bot_token.partition(":")
        return f"{bot_id}:***" if bot_id else "***"

    @property
    def provider_settings(self) -> dict[str, str]:
        """Настройки для процесса агента — без секретов Telegram: инференсу
        они не нужны, а лишняя копия токена в чужом процессе не нужна нам."""
        return {k: v for k, v in self.settings.items() if k not in self.SECRET_KEYS}


def load_config(env_path: Path | None = None) -> Config:
    file_values = _parse_env_file(env_path or DEFAULT_ENV_PATH)
    # os.environ важнее файла: так Docker/systemd перекрывают .env
    merged: dict[str, str] = {**file_values, **os.environ}

    def get(name: str, default: str = "") -> str:
        return merged.get(name, default)

    token = get("TELEGRAM_BOT_TOKEN").strip()
    if not token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN не задан. Скопируй .env.example в .env "
            "и вставь токен от @BotFather (или передай переменную окружения)."
        )
    if ":" not in token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN выглядит некорректно: ожидается формат "
            "<bot_id>:<secret>. Проверь, что скопирован токен целиком."
        )

    return Config(
        telegram_bot_token=token,
        transport=get("TELEGRAM_TRANSPORT", "polling").strip().lower() or "polling",
        llm_provider=get("LLM_PROVIDER", "ollama").strip().lower() or "ollama",
        llm_timeout=_parse_positive_int(get("LLM_TIMEOUT"), "LLM_TIMEOUT", 120),
        poll_timeout=_parse_positive_int(get("POLL_TIMEOUT"), "POLL_TIMEOUT", 30),
        agent_workers=_parse_positive_int(get("AGENT_WORKERS"), "AGENT_WORKERS", 1),
        allowed_user_ids=_parse_user_ids(get("ALLOWED_USER_IDS")),
        log_level=get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        settings=merged,
    )
