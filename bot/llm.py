import httpx, json
from . import db
from .config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL

async def explain(q) -> str:
    if q["explanation"]:
        return q["explanation"]
    opts = json.loads(q["options"])
    letters = "ABCD"
    prompt = (
        "You are a concise tutor. Explain why the correct answer is right and briefly why "
        "each other option is wrong. Max 120 words, plain text, no markdown.\n\n"
        f"Question: {q['text']}\n"
        + "\n".join(f"{letters[i]}. {o}" for i, o in enumerate(opts))
        + f"\nCorrect: {letters[q['correct_idx']]}"
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
    await db.save_explanation(q["id"], text)
    return text