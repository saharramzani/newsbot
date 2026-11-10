"""Provider-agnostic LLM call. LLM_PROVIDER = gemini (free tier) | anthropic."""
import threading
import time

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


_pace_lock = threading.Lock()
_last_call = [0.0]


def _gemini_post(body: dict, timeout: int = 180) -> httpx.Response:
    """POST to Gemini, paced under the free-tier requests-per-minute limit, retrying 429/5xx.

    On 429 Gemini says how long to wait (RetryInfo.retryDelay); we wait that long instead of guessing.
    """
    gap = 60.0 / max(1, config.GEMINI_RPM)
    r = None
    for attempt in range(6):
        with _pace_lock:  # calls from parallel threads queue up here
            wait = _last_call[0] + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.monotonic()
        r = httpx.post(GEMINI_URL.format(model=config.GEMINI_MODEL),
                       headers={"x-goog-api-key": config.GEMINI_API_KEY},  # proxy-injected secret may overwrite this
                       json=body, timeout=timeout)
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == 5:
            break
        delay = 5.0 * (attempt + 1)
        try:
            for d in r.json().get("error", {}).get("details", []):
                if str(d.get("@type", "")).endswith("RetryInfo"):
                    delay = float(str(d.get("retryDelay", "")).rstrip("s") or delay) + 1
        except Exception:
            pass
        time.sleep(min(delay, 65))
    if r.status_code == 429:
        raise RuntimeError("سهمیه‌ی Gemini پر شده (429): " + _gemini_error(r))
    r.raise_for_status()
    return r


def _gemini_error(r) -> str:
    try:
        return str(r.json().get("error", {}).get("message", ""))[:300]
    except Exception:
        return r.text[:300]


def _gemini(system, user, search, max_tokens):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens + 8000,  # thinking models spend part of the budget on reasoning
                              "temperature": 0.8},
    }
    if search:
        body["tools"] = [{"google_search": {}}]
    r = _gemini_post(body)
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


def generate_parts(system: str, parts: list, max_tokens: int = 4000) -> str:
    """Blocking multimodal call. parts: str (text) or (bytes, mime_type) (image), in order."""
    import base64
    if config.LLM_PROVIDER == "gemini":
        gparts = [{"text": p} if isinstance(p, str)
                  else {"inlineData": {"mimeType": p[1], "data": base64.b64encode(p[0]).decode()}} for p in parts]
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": gparts}],
                "generationConfig": {"maxOutputTokens": max_tokens + 8000, "temperature": 0.4}}
        r = _gemini_post(body, timeout=240)
        cands = r.json().get("candidates") or []
        if not cands:
            raise RuntimeError("Gemini returned no candidates: " + str(r.json().get("promptFeedback"))[:300])
        return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
    if config.LLM_PROVIDER == "anthropic":
        import anthropic
        content = [{"type": "text", "text": p} if isinstance(p, str)
                   else {"type": "image", "source": {"type": "base64", "media_type": p[1],
                                                     "data": base64.b64encode(p[0]).decode()}} for p in parts]
        resp = anthropic.Anthropic().messages.create(model=config.WRITER_MODEL, max_tokens=max_tokens, system=system,
                                                     messages=[{"role": "user", "content": content}])
        return "".join(b.text for b in resp.content if b.type == "text")
    raise RuntimeError(f"Unknown LLM_PROVIDER: {config.LLM_PROVIDER}")
