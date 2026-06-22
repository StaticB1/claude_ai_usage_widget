from __future__ import annotations
import json
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Union

from email.utils import parsedate_to_datetime

from .config import (APP_ID, APP_VERSION, CONFIG_DIR, CONFIG_FILE,
                     CREDENTIALS_FILE, USAGE_API_URL, write_private_file)


class RateLimitError(Exception):
    def __init__(self, retry_after: Optional[int] = None,
                 message: str = "Rate limited"):
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message or f"HTTP {code}")
        self.code = code


class CloudApiError(Exception):
    pass


def load_token(credentials_file: Optional[Union[str, Path]] = None
               ) -> Optional[str]:
    """Read OAuth token. Source of truth is the per-account
    ``.credentials.json`` (default ``~/.claude/.credentials.json``), managed
    by the `claude` CLI. Our config is only consulted as a fallback for
    users who paste a token manually (e.g. headless box without `claude`).
    Previously we copied the credentials file into our own config and that
    led to drift when `claude` rotated the token."""
    explicit = credentials_file is not None
    creds_path = Path(credentials_file) if explicit else CREDENTIALS_FILE
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text())
            tok = (data.get("claudeAiOauth") or {}).get("accessToken")
            if tok:
                return tok
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    if not explicit and CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            return data.get("oauth_token")
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    return None


def save_token(token: str) -> None:
    """Persist a manually-pasted token to our own config (NOT to
    .credentials.json — that file belongs to the `claude` CLI)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = {}
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            pass
    config["oauth_token"] = token
    write_private_file(CONFIG_FILE, json.dumps(config, indent=2))


def load_subscription_info(credentials_file: Optional[Union[str, Path]] = None
                           ) -> Optional[dict]:
    creds_path = Path(credentials_file) if credentials_file else CREDENTIALS_FILE
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text())
            oauth = data.get("claudeAiOauth", {})
            if oauth:
                return {
                    "type": oauth.get("subscriptionType", "").title(),
                    "tier": oauth.get("rateLimitTier", ""),
                }
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    return None


def subscription_summary(info: Optional[dict]) -> Tuple[str, bool]:
    """(human-readable plan, is_flat_fee_subscription)."""
    if not info:
        return ("Pay-as-you-go API", False)
    sub_type = (info.get('type') or '').strip()
    rate_tier = (info.get('tier') or '').lower()
    if 'max_20x' in rate_tier:
        return ("Max 20x plan", True)
    if 'max_5x' in rate_tier or 'max' in rate_tier:
        return ("Max 5x plan", True)
    if sub_type.lower() == 'pro':
        return ("Pro plan", True)
    if sub_type.lower() == 'team':
        return ("Team plan", True)
    if sub_type.lower() in ('free', ''):
        return ("Pay-as-you-go API", False)
    return (sub_type or "Pay-as-you-go API", False)


def normalize_utilization(raw) -> float:
    """API returns either a 0..1 fraction or an integer percent — coerce
    to 0..1 float clamped to that range."""
    try:
        v = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if v > 1:
        v = v / 100.0
    return max(0.0, min(v, 1.0))


def fetch_cloud_usage(token: str) -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"{APP_ID}/{APP_VERSION}",
    }
    req = urllib.request.Request(USAGE_API_URL, headers=headers, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError(
                retry_after=_parse_retry_after(e.headers.get('Retry-After')))
        if e.code in (401, 403):
            raise AuthError(e.code, e.reason or "")
        raise CloudApiError(f"HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as e:
        raise CloudApiError(f"Network error: {e}")
    except json.JSONDecodeError as e:
        raise CloudApiError(f"Malformed response: {e}")
    if not isinstance(data, dict):
        raise CloudApiError("Malformed response: expected a JSON object")
    return data


def _parse_retry_after(raw: Optional[str]) -> Optional[int]:
    """RFC 7231 Retry-After is either delta-seconds or an HTTP-date."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def format_reset_time(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "unknown"
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = reset_dt - datetime.now(timezone.utc)
        total_sec = int(delta.total_seconds())
        if total_sec <= 0:
            return "any moment"
        days, rem = divmod(total_sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"
    except (ValueError, TypeError):
        return iso_str
