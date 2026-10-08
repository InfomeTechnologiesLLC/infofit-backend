"""Server-side Gemini adapter for IVA chat and photo descriptions."""
import json
import re
import urllib.error
import urllib.request

from django.conf import settings
from rest_framework.exceptions import APIException


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


def _request_body(messages):
    instructions = []
    contents = []
    for message in messages:
        role = message["role"]
        if role == "system":
            instructions.append(message["content"])
        elif role in ("user", "assistant"):
            contents.append({"role": "model" if role == "assistant" else "user", "parts": _parts(message["content"])})
        else:
            raise ValueError("Unsupported message role")
    body = {"contents": contents, "generationConfig": {"maxOutputTokens": 400, "temperature": 0.3}}
    if instructions:
        body["systemInstruction"] = {"parts": [{"text": "\n".join(instructions)}]}
    return body


def complete(messages, model):
    key = settings.GEMINI_API_KEY
    if not key or not model:
        raise AiUnavailable("IVA is not configured on this server.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise AiUnavailable("IVA model configuration is invalid.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request = urllib.request.Request(
        url,
        data=json.dumps(_request_body(messages)).encode("utf-8"),
        method="POST",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = json.load(response)
        parts = payload["candidates"][0]["content"]["parts"]
        answer = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not answer:
            raise ValueError("Empty model response")
        return answer
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        # Provider response bodies may contain account or request details. Never relay them.
        raise AiUnavailable() from exc
