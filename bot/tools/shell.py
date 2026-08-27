"""Универсальный инструмент exec: выполнение консольных команд.

Через него агент делает всё остальное — curl к REST API, чтение файлов,
локальные CLI-утилиты. Отдельный инструмент под каждую задачу не нужен.

Ограничения (EXEC_* в .env): таймаут, объём вывода, рабочий каталог,
опциональный allowlist бинарей и стоп-лист заведомо разрушительных команд.
Это перила от случайной беды и галлюцинации модели, а НЕ песочница:
команда выполняется с правами процесса бота. Настоящая изоляция —
контейнер (см. Dockerfile) или отдельный пользователь без sudo.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from typing import Any

from bot.core.contracts import Tool, ToolError
from bot.core.text import truncate
from bot.tools import register

logger = logging.getLogger("agent.tools.exec")

#: Команды, которые не должны выполниться даже по ошибке модели.
DENIED: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rR][a-zA-Z]*f?\s+(/|~|\$HOME)\s*($|[|;&])"),
     "рекурсивное удаление корня или домашнего каталога"),
    (re.compile(r"\bmkfs(\.|\b)|\bfdisk\b|\bdiskutil\s+erase"), "форматирование диска"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/"), "запись поверх устройства"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "выключение машины"),
    (re.compile(r":\(\)\s*\{.*\|.*&.*\}"), "fork-бомба"),
    (re.compile(r"\bchmod\b[^|;&]*\s(777|-R\s+777)\s+/(\s|$)"), "открытие прав на корень"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh"), "запуск скрипта из сети"),
    (re.compile(r"(^|[\s/])\.env\b"), "чтение файла с секретами"),
    (re.compile(r"\bsudo\b|\bsu\s+-"), "повышение привилегий"),
)

#: Переменные окружения, которые не уходят в дочерний процесс.
SECRET_ENV = re.compile(r"TOKEN|SECRET|API_KEY|PASSWORD|CREDENTIAL", re.IGNORECASE)

#: Разделители, по которым команда бьётся на части при проверке allowlist.
_SPLIT = re.compile(r"[;|&]+|\$\(|\breturn\b|\bthen\b|\bdo\b")


@register("exec")
class ExecTool(Tool):
    name = "exec"
    description = (
        "выполняет команду в shell и возвращает её вывод (stdout+stderr) и код возврата. "
        "Через него доступны curl, cat, ls, date, python3 и любые CLI-утилиты"
    )
    usage = '{"tool": "exec", "args": {"command": "curl -s https://wttr.in/Minsk?0"}}'

    def __init__(self, settings: Mapping[str, str]):
        super().__init__(settings)
        self._timeout = int(settings.get("EXEC_TIMEOUT", "20") or 20)
        self._max_output = int(settings.get("EXEC_MAX_OUTPUT", "4000") or 4000)
        self._workdir = settings.get("EXEC_WORKDIR", "").strip() or os.getcwd()
        raw_allowed = settings.get("EXEC_ALLOWED_BINARIES", "")
        self._allowed = {c.strip() for c in raw_allowed.split(",") if c.strip()}

    # --- проверки ---------------------------------------------------------

    @staticmethod
    def _denied_reason(command: str) -> str:
        for pattern, reason in DENIED:
            if pattern.search(command):
                return reason
        return ""

    def _not_allowed(self, command: str) -> str:
        """Best-effort allowlist: имя бинаря в начале каждой части команды.

        Полноценно разобрать shell регуляркой нельзя, поэтому проверка
        намеренно строгая: что не распозналось — то запрещено.
        """
        if not self._allowed:
            return ""
        for part in _SPLIT.split(command):
            part = part.strip()
            if not part:
                continue
            try:
                tokens = shlex.split(part)
            except ValueError:
                return "не удалось разобрать команду"
            for token in tokens:
                if "=" in token.split(" ")[0] and not token.startswith("-"):
                    continue  # VAR=value перед командой
                binary = os.path.basename(token)
                if binary not in self._allowed:
                    return f"{binary!r} нет в EXEC_ALLOWED_BINARIES"
                break
        return ""

    def _child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        env["AGENT_EXEC"] = "1"  # чтобы вызванный скрипт знал, кто его дёрнул
        return env

    # --- выполнение -------------------------------------------------------

    def run(self, args: Mapping[str, Any]) -> str:
        command = str(args.get("command") or args.get("cmd") or "").strip()
        if not command:
            raise ToolError("не задан аргумент command")

        reason = self._denied_reason(command)
        if reason:
            logger.warning("Заблокирована команда (%s): %s", reason, command)
            raise ToolError(
                f"команда заблокирована политикой безопасности ({reason}). "
                f"Выполнять её нельзя — реши задачу иначе"
            )
        reason = self._not_allowed(command)
        if reason:
            raise ToolError(f"команда запрещена: {reason}")

        timeout = min(int(args.get("timeout") or self._timeout), self._timeout)
        logger.info("exec: %s", command)
        try:
            completed = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                cwd=self._workdir,
                env=self._child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"команда не завершилась за {timeout} с и была снята. "
                f"Попробуй что-то быстрее или добавь ограничение вывода"
            ) from None
        except OSError as exc:
            raise ToolError(f"не удалось запустить команду: {exc}") from None

        output = (completed.stdout or "") + (completed.stderr or "")
        output = truncate(output.strip(), self._max_output, "вывод обрезан")
        if not output:
            output = "(пустой вывод)"
        return f"exit code: {completed.returncode}\n{output}"
