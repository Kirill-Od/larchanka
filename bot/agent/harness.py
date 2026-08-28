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
from bot.core.contracts import EmptyAnswer, LLMProvider, Message, Tool, ToolError
from bot.core.text import strip_reasoning, truncate

logger = logging.getLogger("agent.harness")

#: Сколько шагов делать, если не задано иное. Задание требует 5–10.
DEFAULT_MAX_STEPS = 8
#: Верхняя граница: больше — это уже не «минимальный агент», а способ сжечь CPU.
MAX_ALLOWED_STEPS = 10
#: Сколько раз подряд терпим один и тот же вызов, прежде чем оборвать цикл.
MAX_REPEATS = 2
#: Сколько пустых ответов модели терпим, прежде чем свернуть задачу.
MAX_EMPTY_REPLIES = 2
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
- Инструменты нужны, чтобы УЗНАТЬ то, чего ты не знаешь. То, что ты хочешь \
сказать пользователю, пиши обычным текстом: не заворачивай свои же слова \
в `echo` и не выполняй команду ради готовой фразы.
- Скилл читай один раз за задачу: прочитанная инструкция уже в контексте.
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

#: Приклеивается к любой неудаче инструмента. Мелкая модель, получив ошибку,
#: охотно закрывает задачу правдоподобным текстом вместо второй попытки —
#: напоминание о том, что сбой это не данные, стоит дешевле лишнего шага.
ERROR_NUDGE = (
    "Это сбой вызова, а не данные. Исправь вызов и повтори — имя бери из "
    "списка доступных выше. Подставлять вместо результата правдоподобные "
    "значения запрещено: пока инструмент не отработал, данных у тебя нет."
)

#: Ответ на повторный вызов. Инструмент при этом не запускается: его результат
#: уже лежит в контексте, а повторный прогон стоит шага и раздувает контекст —
#: тело скилла, прочитанное дважды, это лишние 2800 символов.
REPEAT_NOTE = (
    "ОШИБКА: этот вызов ты уже делал, его результат лежит выше в контексте — "
    "второй раз он не выполнялся. Не повторяйся: переходи к следующему шагу, "
    "а если данных уже достаточно — дай финальный ответ обычным текстом."
)

#: Ответ на пустую реплику модели. Reasoning-модель способна потратить весь
#: бюджет генерации на размышления и не написать ни слова: просим короче.
EMPTY_NUDGE = (
    "Твой прошлый ответ пришёл пустым: весь бюджет генерации ушёл на "
    "размышления. Не рассуждай — напиши ответ сразу, коротко, обычным текстом."
)

#: Показывается модели, когда она «отвечает» после единственного упавшего
#: вызова: собранных данных в этот момент нет, значит ответ выдуман.
FABRICATION_GUARD = (
    "СТОП: ни один инструмент ещё не отработал, последний вызов упал. "
    "Данных для ответа у тебя нет — значит ответ выше выдуман, "
    "пользователь его не увидит. Сделай правильный вызов инструмента сейчас. "
    "Если инструментами задача не решается — так и скажи, честно и без цифр."
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
    #: answer — модель ответила сама; limit / deadline / repeat / empty —
    #: сработал предохранитель.
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


def _failure(detail: str) -> str:
    """Единый вид неудачи инструмента: причина плюс запрет на выдумку."""
    return f"ОШИБКА: {detail}\n{ERROR_NUDGE}"


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
        #: Отработал ли хоть один инструмент — от этого зависит, есть ли
        #: у модели вообще данные для финального ответа.
        tool_succeeded = False
        last_call_failed = False
        guarded = False
        empty_replies = 0

        for step in range(1, self.max_steps + 1):
            out_of_time = time.monotonic() >= deadline
            last_step = step == self.max_steps or out_of_time
            if last_step:
                # Инструменты отключены: просим закрыть задачу тем, что есть.
                context.append(Message("system", FINAL_NUDGE))
                stopped = "deadline" if out_of_time else "limit"

            logger.debug("Шаг %d/%d", step, self.max_steps)
            try:
                reply = strip_reasoning(self.provider.chat(context))
            except EmptyAnswer:
                # Осечка модели, а не отказ провайдера: собранные инструментами
                # данные при ней целы, и выбрасывать их из-за одной пустой
                # реплики нельзя — переспрашиваем.
                empty_replies += 1
                logger.warning(
                    "Пустой ответ модели на шаге %d (%d из %d)",
                    step, empty_replies, MAX_EMPTY_REPLIES,
                )
                if empty_replies >= MAX_EMPTY_REPLIES:
                    break
                context.append(Message("system", EMPTY_NUDGE))
                continue

            call = None if last_step else parse_tool_call(reply)

            if call is None and not last_step and looks_like_tool_call(reply):
                # Оборванное действие — не ответ. Возвращаем модели её ошибку,
                # чтобы она переписала вызов, а не показываем битый JSON человеку.
                logger.warning("Не разобран вызов инструмента на шаге %d", step)
                last_call_failed = True
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
                if not last_step and last_call_failed and not tool_succeeded and not guarded:
                    # Единственный вызов упал, а модель уже «ответила»: взяться
                    # данным неоткуда, это выдумка. Даём один шанс исправиться.
                    logger.warning("Ответ после упавшего вызова — похоже на выдумку")
                    guarded = True
                    # В trace не кладём намеренно: выдуманный ответ не должен
                    # попасть в историю чата. Модели он нужен, человеку — нет.
                    context.append(Message("assistant", reply.strip()))
                    context.append(Message("system", FABRICATION_GUARD))
                    continue

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
            repeats = history.count(call.signature())
            if repeats > MAX_REPEATS:
                logger.warning("Модель зациклилась на вызове %s", call.short())
                text = (
                    "Похоже, я застрял: повторяю один и тот же вызов "
                    f"({call.short()}) и не двигаюсь дальше. Переформулируй задачу."
                )
                trace.append(Message("assistant", text))
                return AgentRun(text, tuple(trace), step, "repeat")

            if repeats > 1:
                # Второй раз то же самое: данные у модели уже есть, ей не хватает
                # не результата, а толчка к следующему шагу. Инструмент не трогаем.
                logger.info("Повтор вызова %s: результат уже в контексте", call.short())
                trace.append(Message("assistant", reply.strip()))
                trace.append(Message("tool", REPEAT_NOTE))
                context.append(Message("assistant", reply.strip()))
                context.append(Message("tool", REPEAT_NOTE))
                continue

            logger.info("Шаг %d: вызов %s", step, call.short())
            if self.on_step is not None:
                self.on_step(call.short())

            observation, ok = self._invoke(call)
            tool_succeeded = tool_succeeded or ok
            last_call_failed = not ok
            trace.append(Message("assistant", reply.strip()))
            trace.append(Message("tool", observation))
            context.append(Message("assistant", reply.strip()))
            context.append(Message("tool", observation))

        # Единственный выход из цикла без ответа: модель молчит раз за разом.
        # Отдаём это как честную осечку, а не как отказ агента.
        text = (
            "Собрал данные, но модель не смогла оформить ответ: она возвращает "
            "пустой текст. Повтори запрос — обычно со второго раза проходит."
        )
        trace.append(Message("assistant", text))
        return AgentRun(text, tuple(trace), self.max_steps, "empty")

    def _invoke(self, call: ToolCall) -> tuple[str, bool]:
        """Возвращает наблюдение для модели и признак того, что вызов удался."""
        tool = self.tools.get(call.name)
        if tool is None:
            available = ", ".join(self.tools) or "—"
            return _failure(f"инструмента {call.name!r} нет. Доступны: {available}"), False
        try:
            result = tool.run(call.args)
        except ToolError as exc:
            return _failure(str(exc)), False
        except Exception as exc:  # noqa: BLE001 — сбой инструмента не роняет цикл
            logger.exception("Инструмент %s упал", call.name)
            return _failure(f"инструмент {call.name} упал ({exc.__class__.__name__})"), False
        return truncate(str(result), MAX_OBSERVATION_CHARS, "вывод обрезан"), True
