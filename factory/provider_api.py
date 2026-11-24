"""Fixed-endpoint Google embeddings and Sail Responses contracts."""

import base64
import json
import math
import re
import struct
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

GOOGLE_MODEL = "gemini-embedding-2"
GOOGLE_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GOOGLE_MODEL}:embedContent"
DIMENSION = 768
SAIL_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
SAIL_URL = "https://api.sailresearch.com/v1/responses"
TIERS = ("balanced", "flex")


class ProviderError(ValueError):
    def __init__(self, message, status=None, retry=None):
        super().__init__(message)
        self.status, self.retry = status, retry


class OutputLimit(ProviderError):
    def __init__(self, payload=None):
        super().__init__("Sail reached its output token limit.", 422)
        self.payload = payload


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def retry_after(value):
    try:
        return max(1, int(value))
    except (ValueError, TypeError):
        try:
            return max(1, math.ceil((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 8


def request(url, headers, payload=None, timeout=40):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, body, dict(headers, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            return json.loads(response.read(4 * 1024**2))
    except urllib.error.HTTPError as error:
        retry = retry_after(error.headers.get("Retry-After")) if error.code in (408, 429, 500, 502, 503, 504) else None
        raise ProviderError(f"Provider returned HTTP {error.code}.", error.code, retry) from None
    except (OSError, TimeoutError):
        raise ProviderError("Provider connection timed out or was unavailable.", retry=8) from None
    except (ValueError, TypeError):
        raise ProviderError("Provider returned an unreadable response.") from None


def embed(key, *, text=None, video=None, send=None):
    send = send or request
    if (text is None) == (video is None):
        raise ValueError("Embed one text or one video at a time.")
    part = {"text": text} if text is not None else {"inlineData": {
        "mimeType": "video/mp4", "data": base64.b64encode(video).decode("ascii")}}
    result = send(GOOGLE_URL, {"x-goog-api-key": key}, {
        "model": f"models/{GOOGLE_MODEL}", "content": {"parts": [part]}, "outputDimensionality": DIMENSION,
    })
    try:
        vector = result["embedding"]["values"]
        length = math.sqrt(sum(x * x for x in vector))
        if len(vector) == DIMENSION and length > 0 and math.isfinite(length):
            return [x / length for x in vector]
    except (KeyError, ValueError, TypeError, OverflowError):
        pass
    raise ProviderError("Google returned an invalid embedding.")


def validate_tier(tier):
    if tier not in TIERS:
        raise ValueError("Sail tier must be Balanced or Flex.")
    return tier


def sail_submit(key, tier, identity, content, *, max_output=512, effort=None, send=None):
    send = send or request
    validate_tier(tier)
    payload = {"model": SAIL_MODEL, "service_tier": tier, "background": tier == "flex",
               "stream": False, "max_output_tokens": max_output, "input": content}
    if effort is not None:
        payload["reasoning"] = {"effort": effort}
    return sail_result(send(SAIL_URL, {"Authorization": f"Bearer {key}", "Idempotency-Key": identity},
                            payload, timeout=350 if tier == "balanced" else 40), tier)


def sail_poll(key, tier, response_id, send=None):
    send = send or request
    if validate_tier(tier) != "flex" or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", response_id):
        raise ProviderError("Invalid Sail background response reference.")
    return sail_result(send(f"{SAIL_URL}/{response_id}", {"Authorization": f"Bearer {key}"}), tier)


def sail_result(payload, tier):
    if not isinstance(payload, dict):
        raise ProviderError("Sail returned an unreadable response.")
    if payload.get("service_tier") not in (None, tier) or payload.get("model") not in (None, SAIL_MODEL):
        raise ProviderError("Sail returned a different model or tier.")
    status = payload.get("status")
    identity = payload.get("id")
    if tier == "flex" and (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identity)):
        raise ProviderError("Sail returned no usable background response ID.")
    if status not in ("queued", "in_progress", "completed"):
        incomplete = payload.get("incomplete_details")
        code = (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None
        if status == "incomplete" and isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens":
            raise OutputLimit(payload)
        if status == "failed":
            raise ProviderError("Sail failed to generate a response.", 422 if code != "server_error" else 503)
        raise ProviderError("Sail did not complete the response.")
    if tier == "balanced" and status != "completed":
        raise ProviderError("Sail did not complete the Balanced response.")
    return payload


def response_text(payload):
    return "".join(part.get("text", "") for output in payload.get("output", [])
                   if output.get("type") == "message" for part in output.get("content", [])
                   if part.get("type") == "output_text")


def image_part(mime, data):
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}


def probe_image(rgb):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes(rgb) * 32) * 32)) + chunk(b"IEND", b""))
