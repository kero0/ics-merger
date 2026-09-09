import re
import stat
import unicodedata
from io import StringIO
from pathlib import Path
from typing import cast

import yaml
from dotenv import dotenv_values
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
_MAX_ENV_BYTES = 64 * 1024
_GOOGLE_CLIENT_ID = "ICS_MERGER_GOOGLE_CLIENT_ID"
_GOOGLE_CLIENT_SECRET = "ICS_MERGER_GOOGLE_CLIENT_SECRET"
_MICROSOFT_CLIENT_ID = "ICS_MERGER_MICROSOFT_CLIENT_ID"
_MICROSOFT_CLIENT_SECRET = "ICS_MERGER_MICROSOFT_CLIENT_SECRET"


class ConfigurationError(ValueError):
    """A configuration error that is safe to show to an operator."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ServerConfig(_StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")


class _NamedCalendar(_StrictModel):
    name: str
    ttl_seconds: float = Field(default=3600.0, gt=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name or any(unicodedata.category(character) == "Cc" for character in name):
            raise ValueError("Calendar name must be non-empty and contain no control characters")
        return name


class RemoteCalendar(_NamedCalendar):
    url: str = Field(min_length=1)


class ProviderCalendar(_NamedCalendar):
    calendar_id: str

    @field_validator("calendar_id")
    @classmethod
    def validate_calendar_id(cls, value: str) -> str:
        calendar_id = value.strip()
        if not calendar_id:
            raise ValueError("Calendar ID must be non-empty")
        return calendar_id


class _CalendarConfig(_StrictModel):
    future_horizon_days: int = Field(default=90, ge=1, le=366)
    include_free_time: bool = True


class _RemoteIcsConfig(_StrictModel):
    calendars: list[RemoteCalendar] = Field(default_factory=list[RemoteCalendar])
    allow_private_networks: bool = False
    max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=10.0, gt=0)
    write_timeout_seconds: float = Field(default=5.0, gt=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)


class _StorageConfig(_StrictModel):
    token_file_path: Path = Path("~/.local/share/ics-merger/tokens.json")


class _GoogleConfig(_StrictModel):
    calendars: list[ProviderCalendar]


class _MicrosoftConfig(_StrictModel):
    tenant: str
    calendars: list[ProviderCalendar]


class ConfigDocument(_StrictModel):
    env_file: Path | None = None
    server: _ServerConfig = Field(default_factory=_ServerConfig)
    calendar: _CalendarConfig = Field(default_factory=_CalendarConfig)
    remote_ics: _RemoteIcsConfig = Field(default_factory=_RemoteIcsConfig)
    storage: _StorageConfig = Field(default_factory=_StorageConfig)
    google: _GoogleConfig | None = None
    microsoft: _MicrosoftConfig | None = None

    @model_validator(mode="after")
    def validate_env_file(self) -> "ConfigDocument":
        if (self.google is not None or self.microsoft is not None) and self.env_file is None:
            raise ValueError("env_file is required when a provider is configured")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_ics_calendars: list[RemoteCalendar] = Field(default_factory=list[RemoteCalendar])
    allow_private_networks: bool = False
    max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=10.0, gt=0)
    write_timeout_seconds: float = Field(default=5.0, gt=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_calendars: list[ProviderCalendar] = Field(
        default_factory=lambda: [ProviderCalendar(name="Google", calendar_id="primary")]
    )
    microsoft_client_id: str | None = None
    microsoft_client_secret: SecretStr | None = None
    microsoft_tenant: str = "common"
    microsoft_calendars: list[ProviderCalendar] = Field(
        default_factory=lambda: [ProviderCalendar(name="Microsoft", calendar_id="primary")]
    )
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    token_file_path: Path = Path("~/.local/share/ics-merger/tokens.json")
    future_horizon_days: int = Field(default=90, ge=1, le=366)
    include_free_time: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> "Settings":
        if (self.google_client_id is None) != (self.google_client_secret is None):
            raise ValueError("Google client ID and secret must be configured together")
        if (self.microsoft_client_id is None) != (self.microsoft_client_secret is None):
            raise ValueError("Microsoft client ID and secret must be configured together")
        if not _TENANT_PATTERN.fullmatch(self.microsoft_tenant):
            raise ValueError("Microsoft tenant must be a tenant name or identifier")
        if not self.google_calendars:
            raise ValueError("At least one Google calendar is required")
        if not self.microsoft_calendars:
            raise ValueError("At least one Microsoft calendar is required")
        configured: list[_NamedCalendar] = list(self.remote_ics_calendars)
        if self.google_enabled:
            configured.extend(self.google_calendars)
        if self.microsoft_enabled:
            configured.extend(self.microsoft_calendars)
        names = [calendar.name.casefold() for calendar in configured]
        if len(names) != len(set(names)):
            raise ValueError("Calendar names must be unique")
        return self

    @property
    def google_enabled(self) -> bool:
        return self.google_client_id is not None and self.google_client_secret is not None

    @property
    def microsoft_enabled(self) -> bool:
        return self.microsoft_client_id is not None and self.microsoft_client_secret is not None

    def callback_url(self, provider: str) -> str:
        return f"{str(self.public_base_url).rstrip('/')}/api/auth/{provider}/callback"


def load_settings(path: Path) -> Settings:
    config_path = path.expanduser().resolve()
    config = load_config_document(config_path)
    return settings_from_document(config, config_path)


def load_config_document(path: Path) -> ConfigDocument:
    config_path = path.expanduser().resolve()
    raw_config = _read_yaml(config_path)
    try:
        return ConfigDocument.model_validate(raw_config)
    except ValidationError as exc:
        raise ConfigurationError(_validation_message(config_path, exc)) from exc


def settings_from_document(config: ConfigDocument, path: Path) -> Settings:
    config_path = path.expanduser().resolve()
    config_dir = config_path.parent
    token_file_path = _resolve_path(config.storage.token_file_path, config_dir)
    environment = (
        _read_environment(_resolve_path(config.env_file, config_dir))
        if config.env_file is not None
        else {}
    )

    try:
        return Settings(
            host=config.server.host,
            port=config.server.port,
            public_base_url=config.server.public_base_url,
            future_horizon_days=config.calendar.future_horizon_days,
            include_free_time=config.calendar.include_free_time,
            remote_ics_calendars=config.remote_ics.calendars,
            allow_private_networks=config.remote_ics.allow_private_networks,
            max_response_bytes=config.remote_ics.max_response_bytes,
            connect_timeout_seconds=config.remote_ics.connect_timeout_seconds,
            read_timeout_seconds=config.remote_ics.read_timeout_seconds,
            write_timeout_seconds=config.remote_ics.write_timeout_seconds,
            pool_timeout_seconds=config.remote_ics.pool_timeout_seconds,
            token_file_path=token_file_path,
            google_client_id=(
                _required_environment(environment, _GOOGLE_CLIENT_ID)
                if config.google is not None
                else None
            ),
            google_client_secret=(
                SecretStr(_required_environment(environment, _GOOGLE_CLIENT_SECRET))
                if config.google is not None
                else None
            ),
            google_calendars=(
                config.google.calendars
                if config.google is not None
                else [ProviderCalendar(name="Google", calendar_id="primary")]
            ),
            microsoft_client_id=(
                _required_environment(environment, _MICROSOFT_CLIENT_ID)
                if config.microsoft is not None
                else None
            ),
            microsoft_client_secret=(
                SecretStr(_required_environment(environment, _MICROSOFT_CLIENT_SECRET))
                if config.microsoft is not None
                else None
            ),
            microsoft_tenant=config.microsoft.tenant if config.microsoft is not None else "common",
            microsoft_calendars=(
                config.microsoft.calendars
                if config.microsoft is not None
                else [ProviderCalendar(name="Microsoft", calendar_id="primary")]
            ),
        )
    except ValidationError as exc:
        raise ConfigurationError(_validation_message(config_path, exc)) from exc


def _read_yaml(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Cannot read configuration file: {path}") from exc
    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Configuration file is not valid YAML: {path}") from exc
    if config is None:
        raise ConfigurationError(f"Configuration file is empty: {path}")
    if not isinstance(config, dict):
        raise ConfigurationError(f"Configuration root must be a mapping: {path}")
    return cast(dict[str, object], config)


def _resolve_path(path: Path, config_dir: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else config_dir / expanded


def _read_environment(path: Path) -> dict[str, str | None]:
    try:
        file_stat = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise ConfigurationError(f"env_file must reference a regular file: {path}")
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ConfigurationError(f"env_file permissions must be 0600: {path}")
        if file_stat.st_size > _MAX_ENV_BYTES:
            raise ConfigurationError(f"env_file exceeds the 64 KiB size limit: {path}")
        with path.open("rb") as env_file:
            content = env_file.read(_MAX_ENV_BYTES + 1)
    except ConfigurationError:
        raise
    except OSError as exc:
        raise ConfigurationError(f"Cannot read env_file: {path}") from exc
    if len(content) > _MAX_ENV_BYTES:
        raise ConfigurationError(f"env_file exceeds the 64 KiB size limit: {path}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"env_file must contain valid UTF-8 text: {path}") from exc
    if "\x00" in text:
        raise ConfigurationError(f"env_file contains a NUL byte: {path}")
    values = dotenv_values(stream=StringIO(text), interpolate=False)
    return dict(values)


def _required_environment(environment: dict[str, str | None], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"env_file must define {name}")
    return value


def _validation_message(path: Path, error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        details.append(f"{location}: {item['msg']}" if location else str(item["msg"]))
    return f"Invalid configuration in {path}: {'; '.join(details)}"
