import json
import urllib.error
import urllib.request

from app.config import Settings
from app.errors import ProviderFailed

_QUOTA_CODES = {"insufficient_quota", "billing_hard_limit_reached"}


def post_json(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except TimeoutError as exc:
        raise ProviderFailed("provider_timeout") from exc
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderFailed("provider_timeout") from exc
        raise ProviderFailed("provider_unavailable") from exc
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderFailed("provider_malformed") from exc
    if not isinstance(body, dict):
        raise ProviderFailed("provider_malformed")
    return body


def _http_error(exc: urllib.error.HTTPError) -> ProviderFailed:
    status, code, message = _error_parts(exc)
    blob = f"{status} {code} {message}".lower()
    if code in _QUOTA_CODES or "billing" in blob or "exceeded your current quota" in blob or "quota exceeded" in blob:
        return ProviderFailed("provider_quota")
    if exc.code == 429 or status == "RESOURCE_EXHAUSTED":
        return ProviderFailed("provider_rate_limited")
    if exc.code >= 500:
        return ProviderFailed("provider_unavailable")
    return ProviderFailed("provider_malformed")


def _error_parts(exc: urllib.error.HTTPError) -> tuple[str, str, str]:
    try:
        payload = json.loads(exc.read().decode())
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return "", "", ""
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "", "", ""
    status = str(error.get("status") or "")
    code = str(error.get("code") or error.get("type") or "")
    message = str(error.get("message") or "")
    return status, code, message
