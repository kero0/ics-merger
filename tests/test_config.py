from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

import pytest
from pydantic import ValidationError

import ics_merger.__main__ as cli
from ics_merger.config import (
    ConfigurationError,
    ProviderCalendar,
    RemoteCalendar,
    Settings,
    load_settings,
)


def write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def write_env(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_provider_defaults_and_disabled_state() -> None:
    settings = Settings()

    assert settings.include_free_time
    assert settings.google_calendars == [ProviderCalendar(name="Google", calendar_id="primary")]
    assert settings.google_calendars[0].ttl_seconds == 3600
    assert settings.microsoft_calendars == [
        ProviderCalendar(name="Microsoft", calendar_id="primary")
    ]
    assert settings.microsoft_tenant == "common"
    assert not settings.google_enabled
    assert not settings.microsoft_enabled


def test_callback_url_uses_api_prefix() -> None:
    settings = Settings.model_validate({"public_base_url": "https://calendar.example/base/"})

    assert (
        settings.callback_url("google") == "https://calendar.example/base/api/auth/google/callback"
    )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"google_client_id": "client"}, "Google client ID and secret"),
        ({"microsoft_client_secret": "secret"}, "Microsoft client ID and secret"),
        ({"microsoft_tenant": "common/path"}, "Microsoft tenant"),
        ({"future_horizon_days": 0}, "greater than or equal to 1"),
    ],
)
def test_rejects_incoherent_programmatic_configuration(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(values)


@pytest.mark.parametrize("name", ["", "  ", "Work\nCalendar"])
def test_rejects_invalid_calendar_names(name: str) -> None:
    with pytest.raises(ValidationError, match="Calendar name"):
        RemoteCalendar(name=name, url="https://calendar.example/feed.ics")


def test_rejects_blank_provider_calendar_id() -> None:
    with pytest.raises(ValidationError, match="Calendar ID"):
        ProviderCalendar(name="Work", calendar_id="  ")


def test_calendar_ttl_is_configurable_and_positive() -> None:
    calendar = RemoteCalendar(
        name="Work",
        url="https://calendar.example/feed.ics",
        ttl_seconds=120,
    )

    assert calendar.ttl_seconds == 120
    with pytest.raises(ValidationError, match="greater than 0"):
        RemoteCalendar(
            name="Invalid",
            url="https://calendar.example/feed.ics",
            ttl_seconds=0,
        )


def test_rejects_duplicate_enabled_calendar_names() -> None:
    with pytest.raises(ValidationError, match="Calendar names must be unique"):
        Settings(
            remote_ics_calendars=[
                RemoteCalendar(name="Work", url="https://calendar.example/feed.ics"),
                RemoteCalendar(name="work", url="https://calendar.example/other.ics"),
            ]
        )


def test_rejects_legacy_calendar_lists(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="remote_ics.urls"):
        load_settings(write_config(tmp_path, "remote_ics:\n  urls: []\n"))


def test_loads_nested_yaml_and_resolves_relative_paths(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    write_env(
        secrets / "providers.env",
        "ICS_MERGER_GOOGLE_CLIENT_ID=google-client\n"
        "ICS_MERGER_GOOGLE_CLIENT_SECRET='google-secret'\n"
        "ICS_MERGER_MICROSOFT_CLIENT_ID=microsoft-client\n"
        "ICS_MERGER_MICROSOFT_CLIENT_SECRET=microsoft-secret\n",
    )
    config_path = write_config(
        tmp_path,
        """
server:
  host: 0.0.0.0
  port: 9000
  public_base_url: https://calendar.example
calendar: {future_horizon_days: 120, include_free_time: false}
env_file: secrets/providers.env
remote_ics: {
    calendars: [{name: Remote, url: https://calendar.example/feed.ics, ttl_seconds: 45}],
    allow_private_networks: false,
    max_response_bytes: 1000000,
    connect_timeout_seconds: 1,
    read_timeout_seconds: 2,
    write_timeout_seconds: 3,
    pool_timeout_seconds: 4,
}
storage:
    token_file_path: data/tokens.json
google: {
    calendars: [
        {name: Personal, calendar_id: primary},
        {name: Team, calendar_id: team@example.com},
    ],
}
microsoft: {
    tenant: organizations,
    calendars: [{name: Company, calendar_id: primary}],
}
""",
    )

    settings = load_settings(config_path)

    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.future_horizon_days == 120
    assert not settings.include_free_time
    assert settings.remote_ics_calendars[0].name == "Remote"
    assert settings.remote_ics_calendars[0].url == "https://calendar.example/feed.ics"
    assert settings.remote_ics_calendars[0].ttl_seconds == 45
    assert [calendar.name for calendar in settings.google_calendars] == ["Personal", "Team"]
    assert [calendar.calendar_id for calendar in settings.google_calendars] == [
        "primary",
        "team@example.com",
    ]
    assert settings.microsoft_calendars == [ProviderCalendar(name="Company", calendar_id="primary")]
    assert settings.token_file_path == tmp_path / "data/tokens.json"
    assert settings.google_client_id == "google-client"
    assert settings.google_client_secret is not None
    assert settings.google_client_secret.get_secret_value() == "google-secret"
    assert settings.microsoft_client_secret is not None
    assert settings.microsoft_client_secret.get_secret_value() == "microsoft-secret"
    assert settings.microsoft_client_id == "microsoft-client"
    assert settings.google_enabled
    assert settings.microsoft_enabled


@pytest.mark.parametrize(
    "content",
    [
        "server:\n  prt: 8000\n",
        "unknown_section: {}\n",
    ],
)
def test_rejects_unknown_keys(tmp_path: Path, content: str) -> None:
    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_settings(write_config(tmp_path, content))


def test_rejects_inline_secret_value(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
google:
    client_id: client
    client_secret: must-not-be-inline
    client_secret_file: google.secret
    calendars: [{name: Personal, calendar_id: primary}]
""",
    )

    with pytest.raises(ConfigurationError) as error:
        load_settings(config_path)

    assert "google.client_secret" in str(error.value)
    assert "must-not-be-inline" not in str(error.value)


@pytest.mark.parametrize("content", ["", "   \n", "server: [\n"])
def test_rejects_empty_or_malformed_yaml(tmp_path: Path, content: str) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(write_config(tmp_path, content))


def test_provider_requires_env_file(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
google:
    calendars: [{name: Personal, calendar_id: primary}]
""",
    )

    with pytest.raises(ConfigurationError, match="env_file is required"):
        load_settings(config_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            "google: {}\n",
            "google.calendars",
        ),
        (
            "microsoft:\n  calendars: [{name: Company, calendar_id: primary}]\n",
            "microsoft.tenant",
        ),
    ],
)
def test_provider_sections_require_all_fields(tmp_path: Path, content: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_settings(write_config(tmp_path, content))


def test_missing_environment_value_does_not_expose_secret(tmp_path: Path) -> None:
    private_value = "this-must-never-appear"
    write_env(
        tmp_path / "providers.env",
        f"ICS_MERGER_GOOGLE_CLIENT_ID={private_value}\n",
    )
    config_path = write_config(
        tmp_path,
        "env_file: providers.env\ngoogle:\n  calendars: [{name: Personal, calendar_id: primary}]\n",
    )

    with pytest.raises(ConfigurationError) as error:
        load_settings(config_path)

    assert "ICS_MERGER_GOOGLE_CLIENT_SECRET" in str(error.value)
    assert private_value not in str(error.value)


@pytest.mark.parametrize(
    ("file_setup", "message"),
    [
        ("missing", "Cannot read env_file"),
        ("directory", "must reference a regular file"),
        ("symlink", "must reference a regular file"),
        ("oversized", "exceeds the 64 KiB size limit"),
        ("nul", "contains a NUL byte"),
        ("permissive", "permissions must be 0600"),
    ],
)
def test_rejects_unsafe_env_files(tmp_path: Path, file_setup: str, message: str) -> None:
    secret_path = tmp_path / "providers.env"
    private_value = "private-secret-value"
    if file_setup == "directory":
        secret_path.mkdir()
    elif file_setup == "symlink":
        target = tmp_path / "target.key"
        target.write_text(private_value, encoding="utf-8")
        secret_path.symlink_to(target)
    elif file_setup == "oversized":
        secret_path.write_bytes(b"x" * (64 * 1024 + 1))
    elif file_setup == "nul":
        secret_path.write_text(f"{private_value}\x00rest", encoding="utf-8")
    elif file_setup == "permissive":
        secret_path.write_text(private_value, encoding="utf-8")
        secret_path.chmod(0o644)
    if secret_path.exists() and file_setup not in {"directory", "symlink", "permissive"}:
        secret_path.chmod(0o600)
    config_path = write_config(
        tmp_path,
        "env_file: providers.env\n",
    )

    with pytest.raises(ConfigurationError, match=message) as error:
        load_settings(config_path)

    assert private_value not in str(error.value)


def test_rejects_unreadable_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_path = write_env(tmp_path / "providers.env", "private-secret-value")
    config_path = write_config(
        tmp_path,
        "env_file: providers.env\n",
    )
    original_open = cast(Callable[..., IO[Any]], Path.open)

    def deny_env_open(path: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        if path == secret_path:
            raise PermissionError
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_env_open)

    with pytest.raises(ConfigurationError, match="Cannot read env_file") as error:
        load_settings(config_path)

    assert "private-secret-value" not in str(error.value)


def test_expands_home_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    write_env(
        secrets / "providers.env",
        "ICS_MERGER_GOOGLE_CLIENT_ID=client\nICS_MERGER_GOOGLE_CLIENT_SECRET=secret\n",
    )
    monkeypatch.setenv("HOME", str(home))
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    config_path = write_config(
        config_dir,
        """
storage:
    token_file_path: ~/tokens.json
env_file: ~/secrets/providers.env
google:
    calendars: [{name: Personal, calendar_id: primary}]
""",
    )

    settings = load_settings(config_path)

    assert settings.token_file_path == home / "tokens.json"
    assert settings.google_enabled


def test_cli_loads_requested_config_before_starting_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config(
        tmp_path,
        "server:\n  host: 0.0.0.0\n  port: 8123\n",
    )
    application = object()
    captured: dict[str, object] = {}

    def fake_create_app(settings: Settings, *, config_path: Path) -> object:
        captured["settings"] = settings
        captured["config_path"] = config_path
        return application

    def fake_run(app: object, *, host: str, port: int) -> None:
        captured.update(app=app, host=host, port=port)

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "run_server", fake_run)

    cli.main(["--config", str(config_path), "--host", "127.0.0.1", "--port", "9000"])

    assert captured["app"] is application
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9000
    assert isinstance(captured["settings"], Settings)
    assert captured["config_path"] == config_path


@pytest.mark.asyncio
async def test_server_checks_idle_state_once_per_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = object.__new__(cli.LowWakeupServer)
    ticks: list[int] = []
    delays: list[float] = []

    async def on_tick(counter: int) -> bool:
        ticks.append(counter)
        return len(ticks) == 2

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(server, "on_tick", on_tick)
    monkeypatch.setattr(cli.asyncio, "sleep", sleep)

    await server.main_loop()

    assert ticks == [0, 10]
    assert delays == [1.0]


def test_cli_requires_default_config_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_run_server(*_args: object, **_kwargs: object) -> None:
        del _args, _kwargs
        pytest.fail()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "run_server", unexpected_run_server)

    with pytest.raises(SystemExit) as error:
        cli.main([])

    assert error.value.code == 2
    assert "Cannot read configuration file" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "65536"])
def test_cli_rejects_invalid_port(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--port", port])

    assert error.value.code == 2
    assert "port must be between 1 and 65535" in capsys.readouterr().err
