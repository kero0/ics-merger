import errno
import stat
from pathlib import Path

import pytest

from ics_merger.config import ConfigDocument, ConfigurationError
from ics_merger.config_repository import ConfigConflictError, ConfigRepository


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o640)
    return path


def test_reads_and_saves_validated_config_with_revision(tmp_path: Path) -> None:
    path = write_config(tmp_path, "calendar:\n  future_horizon_days: 30\n")
    repository = ConfigRepository(path)
    stored = repository.read()
    updated = ConfigDocument.model_validate(
        {
            **stored.document.model_dump(mode="json", exclude_none=True),
            "calendar": {"future_horizon_days": 120},
        }
    )

    saved = repository.save(updated, stored.revision)

    assert saved.settings.future_horizon_days == 120
    assert saved.revision != stored.revision
    assert repository.read().document.calendar.future_horizon_days == 120
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_rejects_stale_revision_without_changing_file(tmp_path: Path) -> None:
    path = write_config(tmp_path, "calendar:\n  future_horizon_days: 30\n")
    repository = ConfigRepository(path)
    stored = repository.read()
    original = path.read_bytes()

    with pytest.raises(ConfigConflictError):
        repository.save(stored.document, "stale")

    assert path.read_bytes() == original


def test_validates_environment_before_writing(tmp_path: Path) -> None:
    path = write_config(tmp_path, "calendar:\n  future_horizon_days: 30\n")
    repository = ConfigRepository(path)
    stored = repository.read()
    invalid = ConfigDocument.model_validate(
        {
            **stored.document.model_dump(mode="json", exclude_none=True),
            "env_file": "missing.env",
            "google": {"calendars": [{"name": "Personal", "calendar_id": "primary"}]},
        }
    )
    original = path.read_bytes()

    with pytest.raises(ConfigurationError, match="Cannot read env_file"):
        repository.save(invalid, stored.revision)

    assert path.read_bytes() == original


def test_writes_through_single_file_bind_mount_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path, "calendar:\n  future_horizon_days: 30\n")
    repository = ConfigRepository(path)
    stored = repository.read()
    updated = ConfigDocument.model_validate(
        {
            **stored.document.model_dump(mode="json", exclude_none=True),
            "calendar": {"future_horizon_days": 60},
        }
    )

    def read_only_parent(*args: object, **kwargs: object) -> tuple[int, str]:
        del args, kwargs
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr("ics_merger.config_repository.tempfile.mkstemp", read_only_parent)

    repository.save(updated, stored.revision)

    assert repository.read().settings.future_horizon_days == 60
