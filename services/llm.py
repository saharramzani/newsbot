"""Provider-agnostic LLM call. LLM_PROVIDER = gemini (free tier) | anthropic."""
import httpx

import config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate(system: str, user: str, search: bool = False, max_tokens: int = 4000) -> tuple[str, list[dict]]:
    """Blocking call. Returns (text, sources) where sources = [{'title','uri'}] from search grounding."""
    if config.LLM_PROVIDER == "gemini":
        return _gemini(system, user, search, max_tokens)
    if config.LLM_PROVIDER == "anthropic":
        return _anthropic(system, user, search, max_tokens)
    raise RuntimeError(f"Unknown LLM_PROVIDER: {config.LLM_PROVIDER}")


def _gemini(system, user, search, max_tokens):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.8},
    }
    if search:
        body["tools"] = [{"google_search": {}}]
    r = httpx.post(
        GEMINI_URL.format(model=config.GEMINI_MODEL),
        headers={"x-goog-api-key": config.GEMINI_API_KEY},  # proxy-injected secret may overwrite this
        json=body, timeout=180)
    r.raise_for_status()
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("Gemini returned no candidates: " + str(data.get("promptFeedback", data))[:400])
    cand = cands[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    sources = [{"title": c["web"].get("title", ""), "uri": c["web"].get("uri", "")}
               for c in cand.get("groundingMetadata", {}).get("groundingChunks", []) if "web" in c]
    return text, sources


def _anthropic(system, user, search, max_tokens):
    import anthropic
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user}]
    kw = {"tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]} if search else {}
    for _ in range(5):  # continue if the server-side search loop pauses
        resp = client.messages.create(model=config.WRITER_MODEL if not search else config.SCOUT_MODEL,
                                      max_tokens=max_tokens, system=system, messages=messages, **kw)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    return "".join(b.text for b in resp.content if b.type == "text"), []
