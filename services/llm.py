"""Provider-agnostic LLM call. LLM_PROVIDER = gemini (free tier) | anthropic."""
import threading
import time

import httpx

import config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate(system: str, user: str, search: bool = False, max_tokens: int = 4000) -> tuple[str, list[dict]]:
    """Blocking call. Returns (text, sources) where sources = [{'title','uri'}] from search grounding."""
    if config.LLM_PROVIDER == "gemini":
        try:
            return _gemini(system, user, search, max_tokens)
        except LLMUnavailable:
            if not (config.OPENAI_API_KEY and config.LLM_FALLBACK):
                raise
            return _openai_chat(system, [user], max_tokens), []
    if config.LLM_PROVIDER == "openai":
        return _openai_chat(system, [user], max_tokens), []
    if config.LLM_PROVIDER == "anthropic":
        return _anthropic(system, user, search, max_tokens)
    raise RuntimeError(f"Unknown LLM_PROVIDER: {config.LLM_PROVIDER}")


_pace_lock = threading.Lock()
_last_call = [0.0]


class LLMUnavailable(RuntimeError):
    """Every configured model failed (retired, overloaded, out of quota)."""


_dead_models: set[str] = set()  # answered 404 (retired / not available to this key): skipped from then on
_cool_until: dict[str, float] = {}  # overloaded / out of quota: tried last for a while
COOLDOWN = 600


def gemini_models() -> list[str]:
    """GEMINI_MODEL first, then GEMINI_FALLBACK_MODELS; retired ones skipped, busy ones moved to the end."""
    out = []
    for m in [config.GEMINI_MODEL] + config.GEMINI_FALLBACK_MODELS:
        if m and m not in out and m not in _dead_models:
            out.append(m)
    now = time.monotonic()
    return sorted(out, key=lambda m: _cool_until.get(m, 0) > now)  # stable: keeps the configured order


def _gemini_post(body: dict, timeout: int = 180) -> httpx.Response:
    """POST to Gemini, trying each model in turn.

    Per model: paced under the free-tier requests-per-minute limit, 429/5xx retried a few times (waiting as long as
    Gemini's RetryInfo asks). A model that is retired (404), keeps failing, or is out of quota hands over to the next
    model. If all fail, LLMUnavailable says why for each.
    """
    gap = 60.0 / max(1, config.GEMINI_RPM)
    problems = []
    models = gemini_models()
    for n, model in enumerate(models):
        r = None
        last_resort = n == len(models) - 1
        for attempt in range(3 if last_resort else 1):  # with other models left, move on at once
            with _pace_lock:  # calls from parallel threads queue up here
                wait = _last_call[0] + gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _last_call[0] = time.monotonic()
            try:
                r = httpx.post(GEMINI_URL.format(model=model),
                               headers={"x-goog-api-key": config.GEMINI_API_KEY},  # proxy may inject the real key
                               json=body, timeout=timeout)
            except httpx.TransportError as e:
                problems.append(f"{model}: {type(e).__name__}")
                r = None
                time.sleep(3)
                continue
            if r.status_code not in (429, 500, 502, 503, 504):
                break
            delay = 4.0 * (attempt + 1)
            daily = "PerDay" in r.text  # daily quota: waiting will not help, move on
            try:
                for d in r.json().get("error", {}).get("details", []):
                    if str(d.get("@type", "")).endswith("RetryInfo"):
                        delay = float(str(d.get("retryDelay", "")).rstrip("s") or delay) + 1
            except Exception:
                pass
            if daily or not last_resort or attempt == 2 or delay > 40:
                break
            time.sleep(delay)
        if r is None:
            continue
        if r.status_code == 200:
            return r
        if r.status_code == 404:
            _dead_models.add(model)
        elif r.status_code in (429, 500, 502, 503, 504):
            _cool_until[model] = time.monotonic() + COOLDOWN
        problems.append(f"{model}: {r.status_code} {_gemini_error(r)[:120]}")
        if r.status_code in (400, 401, 403) and "API key" in r.text:
            break  # a key problem, not a model problem
    raise LLMUnavailable("هیچ مدل Gemini جواب نداد — " + " | ".join(problems))


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
    if config.LLM_PROVIDER == "openai":
        return _openai_chat(system, parts, max_tokens)
    if config.LLM_PROVIDER == "gemini":
        gparts = [{"text": p} if isinstance(p, str)
                  else {"inlineData": {"mimeType": p[1], "data": base64.b64encode(p[0]).decode()}} for p in parts]
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": gparts}],
                "generationConfig": {"maxOutputTokens": max_tokens + 8000, "temperature": 0.4}}
        try:
            r = _gemini_post(body, timeout=240)
        except LLMUnavailable:
            if not (config.OPENAI_API_KEY and config.LLM_FALLBACK):
                raise
            return _openai_chat(system, parts, max_tokens)
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


class ImageUnavailable(RuntimeError):
    """Image generation is not available for this key (free tier has zero quota) or failed."""


_image_off_until = [0.0]  # after a 'no quota' answer, skip image calls for a while instead of waiting each time


def generate_image(prompt: str, reference: bytes | None = None, aspect: str = "9:16",
                   fallback_prompt: str | None = None) -> bytes:
    """Image model -> PNG/JPEG bytes. `reference` (optional) is an image the result is based on.
    IMAGE_PROVIDER picks OpenAI or Gemini."""
    if config.IMAGE_PROVIDER == "openai":
        return _openai_image(prompt, reference, aspect, fallback_prompt)
    if time.monotonic() < _image_off_until[0]:
        raise ImageUnavailable("ساخت تصویر با Gemini برای این کلید فعال نیست (Billing لازم است).")
    import base64
    parts = [{"text": prompt}]
    if reference:
        parts.append({"inlineData": {"mimeType": "image/png", "data": base64.b64encode(reference).decode()}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
    url = GEMINI_URL.format(model=config.GEMINI_IMAGE_MODEL)
    r = httpx.post(url, headers={"x-goog-api-key": config.GEMINI_IMAGE_API_KEY}, json=body, timeout=240)
    if r.status_code == 429 and "limit: 0" in r.text:
        _image_off_until[0] = time.monotonic() + 3600
        raise ImageUnavailable("ساخت تصویر با Gemini در پلن رایگان بسته است (سهمیه‌ی صفر)؛ "
                               "برای فعال شدن، Billing را در Google AI Studio روشن کنید.")
    if r.status_code != 200:
        raise ImageUnavailable(f"Gemini تصویر نساخت ({r.status_code}): {_gemini_error(r)}")
    import base64 as b64
    for cand in r.json().get("candidates") or []:
        for p in cand.get("content", {}).get("parts", []):
            if "inlineData" in p:
                return check_image(b64.b64decode(p["inlineData"]["data"]), "gemini")
    raise ImageUnavailable("Gemini تصویری برنگرداند (احتمالاً درخواست را رد کرد).")


OPENAI_HINTS = {
    "must be verified": "حساب OpenAI باید «تأیید سازمان» (Organization verification) شود تا اجازه‌ی این مدل تصویری را "
                        "بدهد: platform.openai.com ← Settings ← Organization ← Verify.",
    "billing": "اعتبار یا پرداخت حساب OpenAI کافی نیست (Billing).",
    "insufficient_quota": "اعتبار حساب OpenAI تمام شده یا هنوز فعال نشده.",
    "invalid_api_key": "کلید OPENAI_API_KEY در .env اشتباه یا باطل شده است.",
    "safety": "OpenAI این درخواست تصویر را به خاطر قوانین محتوایی‌اش رد کرد.",
}


def _openai_image(prompt: str, reference: bytes | None, aspect: str, fallback_prompt: str | None = None) -> bytes:
    """OpenAI-compatible image API (OpenAI itself or a reseller via OPENAI_BASE_URL) -> image bytes.

    With a reference frame the edit endpoint is tried first; if the service does not support it, the image is
    generated from text only (fallback_prompt).
    """
    import base64
    if not config.OPENAI_API_KEY:
        raise ImageUnavailable("OPENAI_API_KEY در .env تنظیم نشده.")
    base = config.OPENAI_BASE_URL
    model = config.OPENAI_IMAGE_MODEL
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
    dalle3 = model.startswith("dall-e-3")
    data = {"model": model, "prompt": prompt, "n": 1,
            "size": "1024x1792" if dalle3 else "1024x1536",  # portrait; the cover crops it to 9:16 or 4:5
            "quality": "standard" if dalle3 else config.OPENAI_IMAGE_QUALITY}
    if dalle3:
        data["response_format"] = "b64_json"
    r = None
    try:
        if reference and not dalle3:  # dall-e-3 has no edit endpoint
            form = {k: str(v) for k, v in data.items()}
            r = httpx.post(f"{base}/images/edits", headers=headers, data=form,
                           files={"image": ("frame.png", reference, "image/png")}, timeout=300)
        if r is None or r.status_code in (400, 404, 405, 415, 422, 501):  # edit not supported: text-only image
            gen = dict(data, prompt=fallback_prompt or prompt)
            r2 = httpx.post(f"{base}/images/generations", headers=headers, json=gen, timeout=300)
            if r is None or r2.status_code == 200 or r.status_code in (404, 405, 501):
                r = r2
    except httpx.TransportError as e:
        raise ImageUnavailable(f"اتصال به سرویس تصویر ({base}) برقرار نشد: {e}")
    if r.status_code != 200:
        try:
            err = r.json().get("error", {})
            err = err if isinstance(err, dict) else {"message": str(err)}
            msg, code = str(err.get("message", "")), str(err.get("code", "") or err.get("type", ""))
        except Exception:
            msg, code = r.text[:300], ""
        hint = next((h for k, h in OPENAI_HINTS.items() if k in (msg + " " + code).lower()), "")
        raise ImageUnavailable(f"سرویس تصویر تصویر نساخت ({r.status_code}): {msg[:250]}" + (f"\n👉 {hint}" if hint else ""))
    item = (r.json().get("data") or [{}])[0]
    if item.get("b64_json"):
        return check_image(base64.b64decode(item["b64_json"]), "b64_json")
    if item.get("url"):
        u = item["url"]
        if u.startswith("data:image"):  # some services return a data URL
            return check_image(base64.b64decode(u.split(",", 1)[1]), "data url")
        got = httpx.get(u, timeout=120, follow_redirects=True)  # image links often redirect to a CDN
        return check_image(got.content, f"url ({got.status_code}, {got.headers.get('content-type', '?')})")
    raise ImageUnavailable("سرویس تصویر، تصویری برنگرداند: " + str(r.json())[:200])


def image_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def check_image(data: bytes, where: str) -> bytes:
    """Make sure we really got an image (not an error page or a redirect body)."""
    if not image_ext(data):
        preview = data[:200].decode("utf-8", "replace").replace("\n", " ")
        raise ImageUnavailable(f"پاسخ سرویس تصویر، تصویر نبود ({where}، {len(data)} بایت): {preview}")
    return data


def _openai_chat(system: str, parts: list, max_tokens: int = 4000) -> str:
    """OpenAI-compatible chat completion (OpenAI or a reseller via OPENAI_BASE_URL); parts: text or (bytes, mime)."""
    import base64
    content = [{"type": "text", "text": p} if isinstance(p, str) else
               {"type": "image_url", "image_url": {"url": f"data:{p[1]};base64,{base64.b64encode(p[0]).decode()}"}}
               for p in parts]
    body = {"model": config.OPENAI_TEXT_MODEL, "max_tokens": max_tokens + 4000,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
    r = None
    for attempt in range(3):
        try:
            r = httpx.post(f"{config.OPENAI_BASE_URL}/chat/completions", headers=headers, json=body, timeout=240)
        except httpx.TransportError as e:
            if attempt == 2:
                raise LLMUnavailable(f"سرویس پشتیبان ({config.OPENAI_BASE_URL}) در دسترس نیست: {e}")
            time.sleep(5)
            continue
        if r.status_code == 400 and "max_tokens" in r.text:  # newer models want max_completion_tokens
            body["max_completion_tokens"] = body.pop("max_tokens", max_tokens + 4000)
            continue
        if r.status_code not in (429, 500, 502, 503, 504):
            break
        time.sleep(5 * (attempt + 1))
    if r is None or r.status_code != 200:
        msg = r.text[:200] if r is not None else "no answer"
        raise LLMUnavailable(f"سرویس پشتیبان ({config.OPENAI_TEXT_MODEL}) جواب نداد: {msg}")
    msg = (r.json().get("choices") or [{}])[0].get("message", {})
    c = msg.get("content") or ""
    if isinstance(c, list):
        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c
