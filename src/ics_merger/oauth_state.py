import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


class InvalidOAuthStateError(Exception):
    """The OAuth transaction is absent, expired, consumed, or for another provider."""


@dataclass(frozen=True, slots=True)
class OAuthTransaction:
    state: str
    code_verifier: str
    code_challenge: str


@dataclass(frozen=True, slots=True)
class _PendingTransaction:
    provider: str
    code_verifier: str
    expires_at: float


class OAuthTransactionStore:
    def __init__(
        self,
        lifetime_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lifetime_seconds = lifetime_seconds
        self._clock = clock
        self._pending: dict[str, _PendingTransaction] = {}

    def create(self, provider: str) -> OAuthTransaction:
        self._discard_expired()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        self._pending[state] = _PendingTransaction(
            provider=provider,
            code_verifier=verifier,
            expires_at=self._clock() + self._lifetime_seconds,
        )
        return OAuthTransaction(state, verifier, challenge.decode())

    def consume(self, provider: str, state: str) -> str:
        pending = self._pending.pop(state, None)
        if pending is None or pending.provider != provider or pending.expires_at <= self._clock():
            raise InvalidOAuthStateError
        return pending.code_verifier

    def _discard_expired(self) -> None:
        now = self._clock()
        self._pending = {
            state: pending for state, pending in self._pending.items() if pending.expires_at > now
        }
