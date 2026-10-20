import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc.hex(), digest)


# Burn comparable CPU when the login doesn't exist, so timing doesn't reveal valid logins.
DUMMY_HASH = hash_password(secrets.token_hex(16))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


class LoginThrottle:
    """In-memory limiter: max `limit` failed attempts per key within `window` seconds."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit = limit
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._fails[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._prune(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


login_throttle = LoginThrottle()  # per IP + login
ip_throttle = LoginThrottle(limit=30)  # per IP, any login
