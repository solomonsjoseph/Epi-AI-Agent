"""Sidebar titles after an upload: the title model must not store a refusal as the title.

The title model only ever sees the typed first message (and, when files are
attached, the fact that they exist). These tests use a stub model, so no
provider is called.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.conversation_history import ConversationHistoryStore, ConversationTitleGenerator


class StubModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.reply)


def _sent_text(model: StubModel) -> str:
    return "\n".join(str(message.content) for message in model.calls[-1])


CURLY_CANT = "can" + "’" + "t"


@pytest.mark.parametrize(
    "reply",
    [
        "I don't have access to the data",
        "I don't see any attached file in your message. Could you please:\n\n1. Upload it",
        "I need more information to help you",
        "Data you " + CURLY_CANT + " see yet",
        "Cannot read attached dataset",
        "No access to uploaded files",
        "Please share the data you would like me to analyze and summarize today",
    ],
)
def test_reply_or_refusal_falls_back_to_the_typed_message(reply: str) -> None:
    model = StubModel(reply)
    title = ConversationTitleGenerator(model).generate(
        "What is the mean age in this dataset?", has_attachments=True
    )
    assert title == "What is the mean age in this dataset?"


def test_refusal_with_only_whitespace_message_uses_attachment_title() -> None:
    model = StubModel("I don't have access to the data")
    assert ConversationTitleGenerator(model).generate("   ", has_attachments=True) == (
        ConversationHistoryStore.fallback_title("")
    )


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("Mean age in the uploaded cohort", "Mean age in the uploaded cohort"),
        ('"Data quality review."', "Data quality review"),
        ("Incidence by district and visit", "Incidence by district and visit"),
    ],
)
def test_real_titles_are_kept(reply: str, expected: str) -> None:
    model = StubModel(reply)
    assert ConversationTitleGenerator(model).generate("Check this data", has_attachments=True) == expected


def test_access_as_a_study_word_falls_back_to_the_typed_message() -> None:
    # Known trade-off: "access" is also an epidemiology word. The title then
    # falls back to the user's own message, which is still a usable title.
    model = StubModel("Access to care by district")
    title = ConversationTitleGenerator(model).generate("Compare access to care by district")
    assert title == "Compare access to care by district"


def test_prompt_says_files_are_attached_but_not_shown() -> None:
    model = StubModel("Mean age in the cohort")
    ConversationTitleGenerator(model).generate("What is the mean age?", has_attachments=True)
    system = str(model.calls[-1][0].content)
    assert "attached data files" in system
    assert "cannot see" in system


def test_prompt_without_files_has_no_attachment_hint() -> None:
    model = StubModel("Mean age in the cohort")
    ConversationTitleGenerator(model).generate("What is the mean age?")
    assert "attached" not in str(model.calls[-1][0].content)


def test_only_the_typed_message_reaches_the_title_model() -> None:
    model = StubModel("Mean age in the cohort")
    ConversationTitleGenerator(model).generate("What is the mean age?", has_attachments=True)
    assert len(model.calls[-1]) == 2
    assert str(model.calls[-1][1].content) == "What is the mean age?"


def test_runtime_passes_only_a_flag_never_file_names(monkeypatch) -> None:
    """The runtime tells the title generator that files exist, and nothing about them."""
    from api import runtime as runtime_module

    seen: dict[str, object] = {}

    class RecordingGenerator:
        def generate(self, text, **kwargs):
            seen["args"] = (text,)
            seen["kwargs"] = kwargs
            return "Mean age in the cohort"

    class Store:
        def set_automatic_title(self, owner, thread, title):
            seen["stored"] = title

    fake = SimpleNamespace(history_store=Store())
    runtime_module.ReportAgentApiRuntime._generate_title(
        fake, "owner", "thread", "What is the mean age?", RecordingGenerator(), True
    )
    assert seen["args"] == ("What is the mean age?",)
    assert seen["kwargs"] == {"has_attachments": True}
    assert seen["stored"] == "Mean age in the cohort"
