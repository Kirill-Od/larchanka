"""Проверка слоя бота: команды, контекст чата, показ шагов.

Telegram и агент подменены заглушками — ни сети, ни процессов, ни модели.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable, Sequence

from bot.agent.pool import AgentAnswer, AgentError
from bot.config import Config
from bot.core.contracts import Message
from bot.core.history import ConversationStore
from bot.handlers import MessageHandler


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append(text)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        pass


class FakeAgent:
    """Отдаёт заготовленный ответ и запоминает, какой контекст получил."""

    def __init__(self, answer: AgentAnswer | None = None, error: str = ""):
        self.answer = answer or AgentAnswer("готово", (Message("assistant", "готово"),), 1)
        self.error = error
        self.seen: list[Sequence[Message]] = []

    def run(self, messages, on_step: Callable[[str], None] | None = None) -> AgentAnswer:
        self.seen.append(list(messages))
        if self.error:
            raise AgentError(self.error)
        if on_step:
            on_step("exec: date")
        return self.answer


def make_config(**overrides) -> Config:
    base = dict(
        telegram_bot_token="1:x", transport="polling", llm_provider="echo",
        llm_timeout=10, poll_timeout=10, agent_workers=1, agent_max_steps=5,
        agent_task_timeout=60, agent_show_steps=True, history_max_messages=40,
        history_max_chars=12000, allowed_user_ids=frozenset(), log_level="INFO",
        settings={},
    )
    return Config(**{**base, **overrides})


def message(text: str, chat_id: int = 1, user_id: int = 7) -> dict:
    return {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}


class HandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.telegram = FakeTelegram()
        self.agent = FakeAgent()
        self.store = ConversationStore()
        self.handler = MessageHandler(
            make_config(), self.telegram, self.agent, self.store, "тест-модель"
        )

    def test_chat_is_one_long_context(self) -> None:
        self.handler.handle_message(message("первый вопрос"))
        self.handler.handle_message(message("второй вопрос"))

        # Второй запрос обязан видеть первый вопрос и ответ на него.
        second = self.agent.seen[1]
        self.assertEqual([m.content for m in second][:3],
                         ["первый вопрос", "готово", "второй вопрос"])

    def test_new_command_resets_context(self) -> None:
        self.handler.handle_message(message("вопрос"))
        self.handler.handle_message(message("/new"))
        self.handler.handle_message(message("следующий"))

        self.assertEqual([m.content for m in self.agent.seen[1]], ["следующий"])
        self.assertTrue(any("Новый чат" in sent for sent in self.telegram.sent))

    def test_contexts_of_different_chats_do_not_mix(self) -> None:
        self.handler.handle_message(message("из чата 1", chat_id=1))
        self.handler.handle_message(message("из чата 2", chat_id=2))
        self.assertEqual([m.content for m in self.agent.seen[1]], ["из чата 2"])

    def test_skills_command_lists_repo_skills(self) -> None:
        self.handler.handle_message(message("/skills"))
        self.assertIn("morning-briefing", self.telegram.sent[0])
        self.assertIn("weather-cli", self.telegram.sent[0])

    def test_steps_are_shown_in_chat(self) -> None:
        self.handler.handle_message(message("сделай"))
        self.assertEqual(self.telegram.sent, ["⚙️ exec: date", "готово"])

    def test_steps_can_be_switched_off(self) -> None:
        handler = MessageHandler(
            make_config(agent_show_steps=False), self.telegram, self.agent, self.store
        )
        handler.handle_message(message("сделай"))
        self.assertEqual(self.telegram.sent, ["готово"])

    def test_failed_run_keeps_the_question_in_context(self) -> None:
        handler = MessageHandler(
            make_config(), self.telegram, FakeAgent(error="модель недоступна"), self.store
        )
        handler.handle_message(message("вопрос"))
        self.assertIn("модель недоступна", self.telegram.sent[0])
        self.assertEqual([m.content for m in self.store.history(1)], ["вопрос"])

    def test_whitelist_blocks_strangers(self) -> None:
        handler = MessageHandler(
            make_config(allowed_user_ids=frozenset({42})),
            self.telegram, self.agent, self.store,
        )
        handler.handle_message(message("выполни команду", user_id=7))
        self.assertIn("закрытый доступ", self.telegram.sent[0])
        self.assertEqual(self.agent.seen, [])


if __name__ == "__main__":
    unittest.main()
