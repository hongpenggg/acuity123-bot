"""Explanation generation.

The LLM only writes explanations — weakness detection stays in SQL (see
`db._weighted_topic`), so a broken or slow provider can never block practice.
"""
from __future__ import annotations

import logging

import httpx

from . import db
from .config import LLM_API_KEY, LLM_BASE_URL, LLM_ENABLED, LLM_MODEL
from .text import letter, parse_options

log = logging.getLogger(__name__)

_TIMEOUT = 30.0
_MAX_TOKENS = 300
_ATTEMPTS = 2

_PROMPT = (
    "You are a concise ophthalmology tutor for medical students. Explain why the "
    "correct answer is right, and briefly why each other option is wrong. "
    "Max 120 words, plain text, no markdown.\n\n"
)


def build_prompt(question) -> str:
    opts = parse_options(question["options"])
    body = "\n".join(f"{letter(i)}. {o}" for i, o in enumerate(opts))
    return (
        f"{_PROMPT}"
        f"Question: {question['text']}\n"
        f"{body}\n"
        f"Correct: {letter(question['correct_idx'])}"
    )


async def explain(question) -> str | None:
    """A written explanation for this question, or None if there isn't one.

    Returns the stored explanation when present — which is the case for the whole
    preclinical bank, so the LLM is never called for it. Returns None when no
    provider is configured, letting the caller say so plainly instead of
    erroring. The cache write is a no-op if someone else won the race.
    """
    if question["explanation"]:
        return question["explanation"]
    if not LLM_ENABLED:
        return None

    prompt = build_prompt(question)
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in range(_ATTEMPTS):
            try:
                response = await client.post(
                    f"{LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={
                        "model": LLM_MODEL,
                        "max_tokens": _MAX_TOKENS,
                        "temperature": 0.2,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                response.raise_for_status()
                text = response.json()["choices"][0]["message"]["content"].strip()
                if not text:
                    raise ValueError("empty completion")
                break
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                last_error = exc
                log.warning("explain attempt %s/%s failed: %s",
                            attempt + 1, _ATTEMPTS, exc)
        else:
            raise RuntimeError("explanation provider unavailable") from last_error

    await db.save_explanation(question["id"], text)
    return text
