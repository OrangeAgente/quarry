"""Optional password authentication for Quarry.

Single-user app → single password, no user table. Auth is OFF when
QUARRY_PASSWORD is unset (the compose file publishes on 127.0.0.1 by default,
so localhost-only remains the default posture) and ON the moment a password is
configured — which is what makes exposing the app on a LAN or behind Tailscale
reasonable.

QUARRY_PASSWORD accepts either a plaintext password or a Werkzeug hash
(pbkdf2:/scrypt: prefix) for those who don't want plaintext in .env:
    python -c "from werkzeug.security import generate_password_hash as g; print(g('...'))"

Deliberately NOT a UI-editable setting: an unauthenticated visitor must never
be able to set (or clear) the password through the Settings page.
"""
import hmac
import threading
import time

from werkzeug.security import check_password_hash

from config import settings

_HASH_PREFIXES = ("pbkdf2:", "scrypt:", "argon2:")

# --- login rate limiting (in-memory; single process by design) ---
_MAX_FAILURES = 5
_WINDOW_S = 15 * 60
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return bool((settings.quarry_password or "").strip())


def verify_password(candidate: str) -> bool:
    secret = (settings.quarry_password or "").strip()
    if not secret:
        return False
    if secret.startswith(_HASH_PREFIXES):
        return check_password_hash(secret, candidate)
    return hmac.compare_digest(secret, candidate)


def is_locked_out(ip: str) -> tuple[bool, int]:
    """(locked, seconds_remaining). Sliding window: N failures in the window
    locks that IP until the oldest failure ages out."""
    now = time.time()
    with _lock:
        recent = [t for t in _failures.get(ip, []) if now - t < _WINDOW_S]
        _failures[ip] = recent
        if len(recent) >= _MAX_FAILURES:
            return True, int(_WINDOW_S - (now - recent[0])) + 1
    return False, 0


def record_failure(ip: str) -> None:
    with _lock:
        _failures.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)
