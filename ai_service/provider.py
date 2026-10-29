"""Server-side Gemini adapter for IVA chat, tools (function calling), streaming and photo descriptions."""
import json
import re
import urllib.error
import urllib.request

from django.conf import settings
from rest_framework.exceptions import APIException

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models/"
PROVIDER_ERRORS = (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError, ValueError,
                   KeyError, IndexError, TypeError, json.JSONDecodeError)


def failure_reason(exc):
    """A short, staff-safe explanation of why Gemini failed (never includes the API key)."""
    if isinstance(exc, urllib.error.HTTPError):
        message = ""
        try:
            message = json.loads(exc.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - the body is optional
            pass
        hints = {400: "the request or API key was rejected", 401: "the API key is not valid",
                 403: "the API key is not allowed to use Gemini (or the API is not enabled)",
                 404: "the model name is not available", 429: "the free quota or rate limit was reached",
                 500: "Google had an internal error", 503: "Gemini is overloaded right now"}
        text = "Gemini answered {} ({})".format(exc.code, hints.get(exc.code, "error"))
        return (text + ": " + " ".join(str(message).split()))[:300] if message else text
    if isinstance(exc, urllib.error.URLError):
        return "The server cannot reach Gemini: {}".format(exc.reason)[:300]
    if isinstance(exc, (TimeoutError, OSError)):
        return "Gemini did not answer in time ({}).".format(type(exc).__name__)
    return "{}: {}".format(type(exc).__name__, exc)[:300]


def record_failure(reason):
    """Remember the latest IVA problem so staff can see it on the portal's Notifications page."""
    try:
        from notifications.models import SchedulerHeartbeat
        SchedulerHeartbeat.beat("iva", ok=False, summary=reason)
    except Exception:  # noqa: BLE001 - recording must never break the request
        pass


class AiUnavailable(APIException):
    status_code = 503
    default_detail = "IVA is unavailable right now. Please try again later."
    default_code = "ai_unavailable"


def _parts(content):
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        raise ValueError("Invalid message content")
    parts = []
    for item in content:
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append({"text": item["text"]})
        elif item.get("type") == "image_url":
            url = item.get("image_url", {}).get("url", "")
            match = re.fullmatch(r"data:(image/jpeg|image/png);base64,([A-Za-z0-9+/=]+)", url)
            if not match:
                raise ValueError("Invalid inline image")
            parts.append({"inline_data": {"mime_type": match.group(1), "data": match.group(2)}})
        else:
            raise ValueError("Unsupported message part")
    return parts


def to_contents(messages):
    """OpenAI-style [{"role", "content"}] -> (system text, Gemini contents)."""
    instructions, contents = [], []
    for message in messages:
        role = message["role"]
        if role == "system":
            instructions.append(message["content"])
        elif role in ("user", "assistant"):
            contents.append({"role": "model" if role == "assistant" else "user", "parts": _parts(message["content"])})
        else:
            raise ValueError("Unsupported message role")
    return "\n".join(instructions), contents


def _body(system, contents, max_tokens=400, temperature=0.3, tools=None, json_mode=False):
    config = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if json_mode:
        config["responseMimeType"] = "application/json"
    body = {"contents": contents, "generationConfig": config}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
    return body


def _request_body(messages):
    """Kept for callers and tests that build a plain request from chat messages."""
    system, contents = to_contents(messages)
    return _body(system, contents)


def _check(model):
    key = settings.GEMINI_API_KEY
    if not key or not model:
        record_failure("GEMINI_API_KEY or GEMINI_MODEL is empty in the server .env file.")
        raise AiUnavailable("IVA is not configured on this server.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise AiUnavailable("IVA model configuration is invalid.")
    return key


def _request(model, body, stream=False):
    key = _check(model)
    url = API_ROOT + model + (":streamGenerateContent?alt=sse" if stream else ":generateContent")
    return urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                  headers={"x-goog-api-key": key, "Content-Type": "application/json"})


def _candidate_parts(payload):
    candidates = payload.get("candidates") or []
    if not candidates:
        return []
    return [p for p in (candidates[0].get("content") or {}).get("parts") or [] if isinstance(p, dict)]


def generate(system, contents, model, tools=None, max_tokens=1024, timeout=35, json_mode=False):
    """One model turn. Returns the raw list of parts (text and/or functionCall)."""
    request = _request(model, _body(system, contents, max_tokens, tools=tools, json_mode=json_mode))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return _candidate_parts(payload)
    except PROVIDER_ERRORS as exc:
        # Provider response bodies may contain account or request details. Never relay them to the app;
        # staff see a short reason on the portal's Notifications page.
        record_failure(failure_reason(exc))
        raise AiUnavailable() from exc


def stream(system, contents, model, tools=None, max_tokens=1024, timeout=60):
    """Yield parts as Gemini streams them (server-sent events). Text arrives in small pieces."""
    request = _request(model, _body(system, contents, max_tokens, tools=tools), stream=True)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                yield from _candidate_parts(json.loads(data))
    except PROVIDER_ERRORS as exc:
        record_failure(failure_reason(exc))
        raise AiUnavailable() from exc


def text_of(parts):
    return "".join(part.get("text", "") for part in parts if isinstance(part.get("text"), str)).strip()


def complete(messages, model, timeout=35, max_tokens=400, json_mode=False):
    """Plain text answer for a list of chat messages."""
    try:
        system, contents = to_contents(messages)
    except (ValueError, KeyError, TypeError) as exc:
        raise AiUnavailable() from exc
    answer = text_of(generate(system, contents, model, max_tokens=max_tokens, timeout=timeout, json_mode=json_mode))
    if not answer:
        raise AiUnavailable()
    return answer


def complete_json(messages, model, timeout=35, max_tokens=600):
    """Ask for a JSON object. Returns a dict, or None if the model returned something else."""
    text = complete(messages, model, timeout=timeout, max_tokens=max_tokens, json_mode=True)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def diagnose(model=None):
    """One tiny real request. Returns (ok, message) in plain words for the portal's Test IVA button."""
    model = model or settings.GEMINI_MODEL
    try:
        _check(model)
        request = _request(model, _body("", [{"role": "user", "parts": [{"text": "Reply with the word OK."}]}],
                                         max_tokens=20))
        with urllib.request.urlopen(request, timeout=20) as response:
            answer = text_of(_candidate_parts(json.load(response)))
    except AiUnavailable as exc:
        return False, str(exc.detail)
    except PROVIDER_ERRORS as exc:
        reason = failure_reason(exc)
        record_failure(reason)
        return False, reason
    try:
        from notifications.models import SchedulerHeartbeat
        SchedulerHeartbeat.beat("iva", ok=True, summary="Test passed")
    except Exception:  # noqa: BLE001
        pass
    return True, "Gemini ({}) answered: {}".format(model, answer or "(empty reply)")
