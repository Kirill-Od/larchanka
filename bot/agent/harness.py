"""Харнесс: агентный цикл «модель → инструмент → модель → … → ответ».

Модель не выполняет действий сама. Она пишет намерение текстом, харнесс его
разбирает, выполняет инструмент и возвращает результат в контекст следующим
сообщением. Так продолжается, пока модель не ответит без вызова инструмента.

Почему протокол текстовый, а не «родной» function calling API:
у мелких локальных моделей его либо нет, либо он работает через раз, а нам
нужен один и тот же цикл на ollama, vLLM и вообще любом провайдере с одним
методом generate(). Разбор намеренно снисходительный — принимаем и
```tool-блок, и <tool>-тег, и просто голый JSON.

Защита от зацикливания — три независимых предохранителя:
  1. жёсткий потолок шагов (AGENT_MAX_STEPS, по умолчанию 8);
  2. общий дедлайн по времени (AGENT_TASK_TIMEOUT);
  3. детектор повтора: один и тот же вызов подряд обрывает цикл.
На последнем шаге инструменты отключаются и у модели просят финальный ответ —
пользователь получает результат, а не сообщение «лимит исчерпан».
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from bot.agent.skills import SkillLibrary
from bot.core.contracts import LLMProvider, Message, Tool, ToolError
from bot.core.text import strip_reasoning, truncate

logger = logging.getLogger("agent.harness")

#: Сколько шагов делать, если не задано иное. Задание требует 5–10.
DEFAULT_MAX_STEPS = 8
#: Верхняя граница: больше — это уже не «минимальный агент», а способ сжечь CPU.
MAX_ALLOWED_STEPS = 10
#: Сколько раз подряд терпим один и тот же вызов, прежде чем оборвать цикл.
MAX_REPEATS = 2
#: Ограничение на результат одного вызова в контексте.
MAX_OBSERVATION_CHARS = 4000

_FENCED = re.compile(r"```(?:tool|json|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL)
_TAGGED = re.compile(r"<tool(?:_call)?[^>]*>\s*(\{.*?\})\s*</tool(?:_call)?>", re.DOTALL)
_ANY_BLOCK = re.compile(r"```.*?```|<tool(?:_call)?[^>]*>.*?</tool(?:_call)?>", re.DOTALL)

_NAME_KEYS = ("tool", "name", "tool_name", "action", "function")
_ARGS_KEYS = ("args", "arguments", "parameters", "params", "input", "tool_input")

# Похоже на вызов, даже если JSON не разобрался. Такой ответ нельзя отдавать
# пользователю как финальный: это оборванное действие, а не ответ.
_LOOKS_LIKE_CALL = re.compile(r'```(?:tool|json|tool_call)|<tool|"(?:tool|tool_name|action)"\s*:')
# Починка самой частой поломки мелких моделей: неэкранированные кавычки внутри
# аргумента, например {"command": "date "+%H:%M""}. Жадный поиск берёт
# последнюю кавычку перед закрывающей скобкой — то есть весь аргумент целиком.
_LOOSE_NAME = re.compile(r'"(?:tool|tool_name|action|name)"\s*:\s*"([\w.\-]+)"')
_LOOSE_ARG = re.compile(r'"(command|cmd|name|skill|query)"\s*:\s*"(.+)"\s*\}', re.DOTALL)

SYSTEM_PROMPT = """Ты — автономный агент в Telegram. Ты не просто отвечаешь \
текстом: у тебя есть инструменты, которыми ты выполняешь реальные действия \
в системе, и ты обязан ими пользоваться, когда данных не хватает.

КАК ТЫ РАБОТАЕШЬ
1. Проверь, хватает ли данных для ответа.
2. Не хватает — вызови ОДИН инструмент и остановись.
3. Результат придёт следующим сообщением «Результат инструмента».
4. Повтори, пока данных не станет достаточно.
5. Достаточно — напиши финальный ответ обычным текстом, без вызовов.

ФОРМАТ ВЫЗОВА
Ровно один блок, и в сообщении не должно быть ничего, кроме него:
```tool
{{"tool": "имя_инструмента", "args": {{"аргумент": "значение"}}}}
```

ЖЁСТКИЕ ПРАВИЛА
- Никогда не придумывай результат вызова: пока ты его не получил, у тебя нет данных.
- Один вызов за сообщение. Не пиши несколько блоков подряд.
- Не повторяй вызов, который уже сделал: результат у тебя уже есть.
- Не показывай пользователю сырой вывод команд — пересказывай его человеческим языком.
- У тебя не больше {max_steps} шагов на задачу. Не трать их на разговоры с собой.
- Если задача решается без инструментов (объяснить, перевести, поболтать) — просто отвечай.

ИНСТРУМЕНТЫ
{tools}

СКИЛЛЫ — готовые инструкции под конкретные задачи{skills_hint}
{skills}

Отвечай по-русски, коротко и по делу. Чат — одна длинная беседа: помни,\
 о чём говорили выше."""

FINAL_NUDGE = (
    "Шаги закончились. Инструменты больше недоступны. Дай финальный ответ "
    "пользователю обычным текстом, опираясь на то, что уже собрал. "
    "Если чего-то узнать не удалось — честно скажи об этом."
)

NO_SKILLS_HINT = " (пока не добавлено ни одного — работай инструментами напрямую)"
SKILLS_HINT = (
    " — если задача похожа на одну из них, ПЕРВЫМ делом прочитай инструкцию "
    "инструментом skill:"
)


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict

    def signature(self) -> str:
        """Ключ для детектора повторов."""
        return f"{self.name}:{json.dumps(self.args, sort_keys=True, ensure_ascii=False)}"

    def short(self) -> str:
        """Однострочное описание для лога и для показа пользователю."""
        if not self.args:
            return self.name
        value = next(iter(self.args.values()))
        return f"{self.name}: {truncate(str(value), 120, 'обрезано')}"


@dataclass(frozen=True)
class AgentRun:
    """Итог одного прогона цикла."""

    text: str
    #: Всё, что цикл добавил в контекст: вызовы, результаты, финальный ответ.
    trace: tuple[Message, ...] = ()
    steps: int = 0
    #: answer — модель ответила сама; limit / deadline / repeat — сработал предохранитель.
    stopped: str = "answer"


# --- разбор ответа модели -------------------------------------------------


def _json_objects(text: str) -> Iterator[str]:
    """Достаёт из текста сбалансированные {...}, не спотыкаясь о скобки в строках."""
    depth = 0
    start = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                yield text[start:index + 1]


def _normalize(payload: object) -> ToolCall | None:
    """Приводит любой разумный вариант записи вызова к ToolCall."""
    if not isinstance(payload, dict):
        return None

    name: object = ""
    for key in _NAME_KEYS:
        if key in payload:
            name = payload[key]
            break
    # Формат OpenAI: {"function": {"name": ..., "arguments": ...}}
    if isinstance(name, dict):
        return _normalize(name)
    if not isinstance(name, str) or not name.strip():
        return None

    args: object = None
    for key in _ARGS_KEYS:
        if key in payload:
            args = payload[key]
            break
    if args is None:
        # Аргументы могли положить прямо в корень: {"tool": "exec", "command": "date"}
        args = {k: v for k, v in payload.items() if k not in _NAME_KEYS}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            # Строка вместо объекта — частая ошибка мелких моделей.
            # Раскладываем её в оба ходовых ключа, инструмент возьмёт свой.
            args = {"command": args, "name": args}
    if not isinstance(args, dict):
        args = {}
    return ToolCall(name.strip().lower(), args)


def parse_tool_call(reply: str) -> ToolCall | None:
    """Ищет в ответе модели вызов инструмента. None — значит это финальный ответ."""
    for pattern in (_FENCED, _TAGGED):
        for match in pattern.finditer(reply):
            call = _normalize(_safe_json(match.group(1)))
            if call:
                return call
    for candidate in _json_objects(reply):
        call = _normalize(_safe_json(candidate))
        if call:
            return call
    return _repair(reply)


def _repair(reply: str) -> ToolCall | None:
    """Последняя попытка: вытащить вызов регулярками из битого JSON."""
    name = _LOOSE_NAME.search(reply)
    argument = _LOOSE_ARG.search(reply)
    if not name or not argument:
        return None
    key, value = argument.group(1), argument.group(2)
    if value.count('"') % 2:
        # Кавычку «съел» разбор — без неё shell не запустится.
        value += '"'
    logger.info("Вызов разобран в щадящем режиме: JSON от модели был битым")
    return ToolCall(name.group(1).strip().lower(), {key: value})


def looks_like_tool_call(reply: str) -> bool:
    """Модель явно пыталась вызвать инструмент, но разобрать не вышло."""
    return bool(_LOOKS_LIKE_CALL.search(reply))


def _safe_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def strip_tool_blocks(reply: str) -> str:
    """Убирает служебные блоки — на случай, если модель смешала их с текстом."""
    cleaned = _ANY_BLOCK.sub("", reply)
    for candidate in _json_objects(cleaned):
        if _normalize(_safe_json(candidate)):
            cleaned = cleaned.replace(candidate, "")
    return cleaned.strip()


# --- цикл -----------------------------------------------------------------


@dataclass
class Harness:
    provider: LLMProvider
    tools: Mapping[str, Tool] = field(default_factory=dict)
    skills: SkillLibrary | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    #: Общий бюджет времени на задачу, секунды.
    time_budget: float = 300.0
    #: Куда сообщать о ходе работы (в Telegram уходит «⚙️ exec: …»).
    on_step: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        self.max_steps = max(1, min(self.max_steps, MAX_ALLOWED_STEPS))

    # --- промпт ---

    def system_prompt(self) -> str:
        tools = "\n".join(
            f"- {tool.name} — {tool.description}"
            + (f"\n  Пример: {tool.usage}" if tool.usage else "")
            for tool in self.tools.values()
        ) or "- (инструментов нет, отвечай своими силами)"

        has_skills = self.skills is not None and len(self.skills) > 0
        return SYSTEM_PROMPT.format(
            max_steps=self.max_steps,
            tools=tools,
            skills_hint=SKILLS_HINT if has_skills else NO_SKILLS_HINT,
            skills=self.skills.catalog() if has_skills else "",
        ).strip()

    # --- выполнение ---

    def run(self, messages: Sequence[Message]) -> AgentRun:
        """Крутит цикл до финального ответа или до срабатывания предохранителя."""
        context: list[Message] = [Message("system", self.system_prompt()), *messages]
        trace: list[Message] = []
        deadline = time.monotonic() + self.time_budget
        history: list[str] = []
        stopped = "answer"

        for step in range(1, self.max_steps + 1):
            out_of_time = time.monotonic() >= deadline
            last_step = step == self.max_steps or out_of_time
            if last_step:
                # Инструменты отключены: просим закрыть задачу тем, что есть.
                context.append(Message("system", FINAL_NUDGE))
                stopped = "deadline" if out_of_time else "limit"

            logger.debug("Шаг %d/%d", step, self.max_steps)
            reply = strip_reasoning(self.provider.chat(context))
            call = None if last_step else parse_tool_call(reply)

            if call is None and not last_step and looks_like_tool_call(reply):
                # Оборванное действие — не ответ. Возвращаем модели её ошибку,
                # чтобы она переписала вызов, а не показываем битый JSON человеку.
                logger.warning("Не разобран вызов инструмента на шаге %d", step)
                observation = (
                    "ОШИБКА: не смог разобрать вызов инструмента. Пришли ровно "
                    "один блок ```tool с корректным JSON: кавычки внутри значения "
                    "надо экранировать обратным слэшем или заменить на одинарные."
                )
                trace.append(Message("assistant", reply.strip()))
                trace.append(Message("tool", observation))
                context.append(Message("assistant", reply.strip()))
                context.append(Message("tool", observation))
                continue

            if call is None:
                text = strip_tool_blocks(reply) if last_step else reply.strip()
                if not text:
                    text = (
                        "Не успел довести задачу до конца за отведённые шаги. "
                        "Уточни запрос или разбей его на части."
                    )
                trace.append(Message("assistant", text))
                return AgentRun(
                    text=text,
                    trace=tuple(trace),
                    steps=step,
                    stopped="answer" if not last_step else stopped,
                )

            history.append(call.signature())
            if history.count(call.signature()) > MAX_REPEATS:
                logger.warning("Модель зациклилась на вызове %s", call.short())
                text = (
                    "Похоже, я застрял: повторяю один и тот же вызов "
                    f"({call.short()}) и не двигаюсь дальше. Переформулируй задачу."
                )
                trace.append(Message("assistant", text))
                return AgentRun(text, tuple(trace), step, "repeat")

            logger.info("Шаг %d: вызов %s", step, call.short())
            if self.on_step is not None:
                self.on_step(call.short())

            observation = self._invoke(call)
            trace.append(Message("assistant", reply.strip()))
            trace.append(Message("tool", observation))
            context.append(Message("assistant", reply.strip()))
            context.append(Message("tool", observation))

        # Сюда не попадаем: последний шаг всегда возвращает ответ.
        raise AssertionError("агентный цикл завершился без ответа")

    def _invoke(self, call: ToolCall) -> str:
        tool = self.tools.get(call.name)
        if tool is None:
            available = ", ".join(self.tools) or "—"
            return f"ОШИБКА: инструмента {call.name!r} нет. Доступны: {available}"
        try:
            result = tool.run(call.args)
        except ToolError as exc:
            return f"ОШИБКА: {exc}"
        except Exception as exc:  # noqa: BLE001 — сбой инструмента не роняет цикл
            logger.exception("Инструмент %s упал", call.name)
            return f"ОШИБКА: инструмент {call.name} упал ({exc.__class__.__name__})"
        return truncate(str(result), MAX_OBSERVATION_CHARS, "вывод обрезан")
