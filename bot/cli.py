"""Агент в терминале: тот же харнесс, но без Telegram.

Нужен, чтобы отлаживать цикл и скиллы, не трогая бота и не тратя апдейты:

    python3 -m bot.cli                       # провайдер из .env
    python3 -m bot.cli --provider echo       # без модели, проверить обвязку
    python3 -m bot.cli "утренняя сводка"     # один вопрос и выход

Здесь цикл крутится прямо в этом процессе (пул процессов не нужен),
поэтому видно каждый шаг и полную трассировку по /trace.
"""

from __future__ import annotations

import argparse
import logging
import sys

from bot import providers, tools
from bot.agent.harness import Harness
from bot.agent.skills import SkillLibrary
from bot.config import ConfigError, load_config
from bot.core.contracts import LLMError, Message
from bot.core.history import ConversationStore

CHAT_ID = 0  # в терминале чат один


def build_harness(args: argparse.Namespace, settings: dict[str, str]) -> Harness:
    provider = providers.create(
        args.provider or settings.get("LLM_PROVIDER", "ollama").lower(),
        settings,
        int(settings.get("LLM_TIMEOUT", "120") or 120),
    )
    return Harness(
        provider=provider,
        tools=tools.create_all(settings),
        skills=SkillLibrary.load(settings.get("SKILLS_DIR", "")),
        max_steps=args.max_steps or int(settings.get("AGENT_MAX_STEPS", "8") or 8),
        time_budget=float(settings.get("AGENT_TASK_TIMEOUT", "300") or 300),
        on_step=lambda text: print(f"  ⚙️  {text}", flush=True),
    )


def ask(harness: Harness, store: ConversationStore, question: str) -> None:
    history = store.history(CHAT_ID)
    user = Message("user", question)
    try:
        run = harness.run([*history, user])
    except LLMError as exc:
        print(f"  ✖ модель недоступна: {exc}", file=sys.stderr)
        return
    store.extend(CHAT_ID, [user, *run.trace])
    print(f"\n{run.text}\n")
    print(f"  [шагов: {run.steps}, завершение: {run.stopped}]\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Минимальный агент в терминале")
    parser.add_argument("question", nargs="*", help="разовый вопрос; без него — диалог")
    parser.add_argument("--provider", help="переопределить LLM_PROVIDER (например echo)")
    parser.add_argument("--max-steps", type=int, help="потолок шагов цикла")
    parser.add_argument("--verbose", action="store_true", help="логи уровня DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    try:
        settings = dict(load_config().settings)
    except ConfigError:
        # В терминале токен Telegram не нужен — работаем на голом окружении.
        from bot.config import DEFAULT_ENV_PATH, _parse_env_file
        import os

        settings = {**_parse_env_file(DEFAULT_ENV_PATH), **os.environ}

    harness = build_harness(args, settings)
    store = ConversationStore()

    if args.question:
        ask(harness, store, " ".join(args.question))
        return 0

    print(
        f"Агент: модель {harness.provider.model}, инструменты "
        f"[{', '.join(harness.tools) or '—'}], потолок {harness.max_steps} шагов.\n"
        "/new — новый чат, /skills — список скиллов, /quit — выход.\n"
    )
    while True:
        try:
            question = input("вы> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question in ("/quit", "/exit"):
            return 0
        if question == "/new":
            print(f"  забыто сообщений: {store.reset(CHAT_ID)}\n")
            continue
        if question == "/skills":
            print(harness.skills.catalog() if harness.skills else "—", "\n")
            continue
        ask(harness, store, question)


if __name__ == "__main__":
    raise SystemExit(main())
