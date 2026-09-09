import json
import os
import stat
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import TypeAlias, cast

TokenValue: TypeAlias = str | int | float | bool | None
Token: TypeAlias = dict[str, TokenValue]
_MAX_TOKEN_BYTES = 1024 * 1024


class TokenStoreError(Exception):
    """A sanitized token storage failure."""


class TokenStore:
    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()
        self._lock = threading.Lock()

    def get(self, provider: str) -> Token | None:
        with self._lock:
            token = self._load().get(provider)
            return dict(token) if token is not None else None

    def set(self, provider: str, token: Token) -> None:
        with self._lock:
            tokens = self._load()
            tokens[provider] = dict(token)
            self._write(tokens)

    def delete(self, provider: str) -> bool:
        with self._lock:
            tokens = self._load()
            if provider not in tokens:
                return False
            del tokens[provider]
            self._write(tokens)
            return True

    def _load(self) -> dict[str, Token]:
        try:
            file_stat = self._path.lstat()
            if self._path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
                raise TokenStoreError("Token store must be a regular file")
            if stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise TokenStoreError("Token store permissions must be 0600")
            if file_stat.st_size > _MAX_TOKEN_BYTES:
                raise TokenStoreError("Token store exceeds the 1 MiB size limit")
            payload = self._path.read_bytes()
        except FileNotFoundError:
            return {}
        except TokenStoreError:
            raise
        except OSError as exc:
            raise TokenStoreError("Unable to read token store") from exc

        try:
            value = json.loads(payload)
            return _validate_tokens(value)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise TokenStoreError("Token store is invalid") from exc

    def _write(self, tokens: dict[str, Token]) -> None:
        try:
            payload = json.dumps(tokens, sort_keys=True, separators=(",", ":")).encode()
            if len(payload) > _MAX_TOKEN_BYTES:
                raise TokenStoreError("Token store exceeds the 1 MiB size limit")
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as temporary_file:
                    temporary_file.write(payload)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_name, self._path)
                os.chmod(self._path, 0o600)
                directory_descriptor = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name)
                raise
        except OSError as exc:
            raise TokenStoreError("Unable to write token store") from exc


def _validate_tokens(value: object) -> dict[str, Token]:
    if not isinstance(value, dict):
        raise ValueError
    raw_tokens = cast(dict[object, object], value)
    tokens: dict[str, Token] = {}
    for provider, raw_token in raw_tokens.items():
        if not isinstance(provider, str) or not isinstance(raw_token, dict):
            raise ValueError
        raw_token_values = cast(dict[object, object], raw_token)
        token: Token = {}
        for key, token_value in raw_token_values.items():
            if not isinstance(key, str) or not (
                isinstance(token_value, str | int | float | bool) or token_value is None
            ):
                raise ValueError
            token[key] = token_value
        tokens[provider] = token
    return tokens
