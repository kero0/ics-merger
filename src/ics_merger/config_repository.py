import errno
import hashlib
import os
import stat
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from ics_merger.config import (
    ConfigDocument,
    ConfigurationError,
    Settings,
    load_config_document,
    settings_from_document,
)


class ConfigConflictError(Exception):
    """Raised when the configuration changed after it was read."""


class ConfigWriteError(Exception):
    """A sanitized configuration persistence failure."""


@dataclass(frozen=True, slots=True)
class StoredConfig:
    document: ConfigDocument
    settings: Settings
    revision: str


class ConfigRepository:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._lock = threading.Lock()

    def read(self) -> StoredConfig:
        with self._lock:
            return self._read()

    def save(self, document: ConfigDocument, expected_revision: str) -> StoredConfig:
        with self._lock:
            current_revision = self._revision()
            if current_revision != expected_revision:
                raise ConfigConflictError
            settings = settings_from_document(document, self.path)
            payload = yaml.safe_dump(
                document.model_dump(mode="json", exclude_none=True),
                sort_keys=False,
                allow_unicode=False,
            ).encode("utf-8")
            self._write(payload)
            return StoredConfig(document, settings, _revision(payload))

    def _read(self) -> StoredConfig:
        document = load_config_document(self.path)
        return StoredConfig(document, settings_from_document(document, self.path), self._revision())

    def _revision(self) -> str:
        try:
            return _revision(self.path.read_bytes())
        except OSError as exc:
            raise ConfigurationError(f"Cannot read configuration file: {self.path}") from exc

    def _write(self, payload: bytes) -> None:
        try:
            current_stat = self.path.lstat()
            if self.path.is_symlink() or not stat.S_ISREG(current_stat.st_mode):
                raise ConfigWriteError("Configuration path must be a regular file")
            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                )
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EROFS}:
                    raise
                self._replace_bind_mount(payload)
                return
            try:
                os.fchmod(descriptor, stat.S_IMODE(current_stat.st_mode))
                with os.fdopen(descriptor, "wb") as temporary_file:
                    temporary_file.write(payload)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                try:
                    os.replace(temporary_name, self.path)
                except OSError as exc:
                    if exc.errno not in {errno.EBUSY, errno.EXDEV}:
                        raise
                    self._replace_bind_mount(payload)
                    os.unlink(temporary_name)
                self._sync_directory()
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name)
                raise
        except ConfigWriteError:
            raise
        except OSError as exc:
            raise ConfigWriteError("Unable to write configuration file") from exc

    def _replace_bind_mount(self, payload: bytes) -> None:
        with self.path.open("wb") as config_file:
            config_file.write(payload)
            config_file.flush()
            os.fsync(config_file.fileno())

    def _sync_directory(self) -> None:
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _revision(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
