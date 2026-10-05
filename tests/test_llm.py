"""Explanation behaviour: the written bank is served first, the LLM is optional."""
import json

import pytest

from bot import llm


def question(explanation=None, n_options=5, correct_idx=4):
    return {
        "id": 1,
        "topic": "Sample",
        "text": "Which structure lies in the outer nuclear layer?",
        "options": json.dumps([f"option {i}" for i in range(n_options)]),
        "correct_idx": correct_idx,
        "explanation": explanation,
    }


@pytest.fixture
def no_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the provider must not be contacted")

    monkeypatch.setattr(llm.httpx, "AsyncClient", explode)


@pytest.mark.asyncio
async def test_stored_explanation_is_served_and_the_provider_is_never_called(no_network):
    text = await llm.explain(question("Written by the content author."))
    assert text == "Written by the content author."


@pytest.mark.asyncio
async def test_returns_none_when_there_is_no_explanation_and_no_provider(no_network, monkeypatch):
    monkeypatch.setattr(llm, "LLM_ENABLED", False)
    assert await llm.explain(question()) is None


def test_prompt_covers_every_option_including_five_option_questions():
    prompt = llm.build_prompt(question(n_options=5, correct_idx=4))
    assert "5. option 4" in prompt
    assert "Correct: 5" in prompt


def test_prompt_marks_the_right_answer():
    prompt = llm.build_prompt(question(n_options=4, correct_idx=2))
    assert "Correct: 3" in prompt
    assert "3. option 2" in prompt
