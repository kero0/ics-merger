from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, cast

import httpx2 as httpx
import pytest
from icalendar.cal import Component
from pydantic import SecretStr

from ics_merger.config import Settings
from ics_merger.merge import CalendarMerger
from ics_merger.oauth import OAuthClientError, OAuthProviderConfig, TokenUpdate, provider_config
from ics_merger.oauth_state import OAuthTransaction
from ics_merger.sources.errors import SourceError, SourceErrorCode
from ics_merger.sources.google import GoogleCalendarSource
from ics_merger.token_store import Token, TokenStore


class DateProperty(Protocol):
    dt: date | datetime


class _ComponentWalker(Protocol):
    def walk(self, name: str | None = None) -> list[Component]: ...


def _walk_components(component: Component, name: str | None = None) -> list[Component]:
    return cast(_ComponentWalker, component).walk(name)


def event_time(event: Component, name: str) -> date | datetime:
    return cast(DateProperty, event[name]).dt


class ScriptedClient:
    def __init__(
        self,
        responses: list[httpx.Response],
        token_update: TokenUpdate | None,
        failure: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.token_update = token_update
        self.failure = failure
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def create_authorization_url(
        self, transaction: OAuthTransaction, parameters: Mapping[str, str]
    ) -> str:
        del transaction, parameters
        raise AssertionError

    async def exchange_code(self, code: str, code_verifier: str) -> Token:
        del code, code_verifier
        raise AssertionError

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        del headers
        self.calls.append((url, params))
        if self.failure is not None:
            raise self.failure
        if len(self.calls) == 1 and self.token_update is not None:
            await self.token_update(
                {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}
            )
        return self.responses.pop(0)

    async def close(self) -> None:
        pass


class ScriptedFactory:
    def __init__(self, responses: list[httpx.Response], failure: Exception | None = None) -> None:
        self.responses = responses
        self.failure = failure
        self.client: ScriptedClient | None = None

    def __call__(
        self,
        config: OAuthProviderConfig,
        token: Token | None = None,
        token_update: TokenUpdate | None = None,
    ) -> ScriptedClient:
        del config, token
        self.client = ScriptedClient(self.responses, token_update, self.failure)
        return self.client


def build_source(
    tmp_path: Path, responses: list[httpx.Response]
) -> tuple[GoogleCalendarSource, TokenStore, ScriptedFactory]:
    settings = Settings(
        google_client_id="client",
        google_client_secret=SecretStr("secret"),
        token_file_path=tmp_path / "tokens.json",
    )
    store = TokenStore(settings.token_file_path)
    store.set("google", {"access_token": "access", "refresh_token": "refresh"})
    config = provider_config(settings, "google")
    assert config is not None
    factory = ScriptedFactory(responses)
    source = GoogleCalendarSource(
        "team/calendar@example.com",
        30,
        store,
        config,
        factory,
        now=lambda: datetime(2026, 9, 5, 12, tzinfo=UTC),
    )
    return source, store, factory


@pytest.mark.asyncio
async def test_google_pagination_and_deterministic_conversion(tmp_path: Path) -> None:
    first = httpx.Response(
        200,
        json={
            "items": [
                {
                    "id": "instance-1",
                    "iCalUID": "timed@example.com",
                    "status": "tentative",
                    "updated": "2026-09-01T10:00:00Z",
                    "start": {"dateTime": "2026-09-06T09:00:00+02:00"},
                    "end": {"dateTime": "2026-09-06T10:00:00+02:00"},
                    "originalStartTime": {"dateTime": "2026-09-06T08:00:00+02:00"},
                    "summary": "Planning",
                    "description": "Internal notes",
                    "location": "Room 1",
                    "transparency": "transparent",
                    "attendees": [{"email": "private@example.com"}],
                    "organizer": {"email": "private@example.com"},
                }
            ],
            "nextPageToken": "next-page",
        },
    )
    second = httpx.Response(
        200,
        json={
            "items": [
                {
                    "id": "ooo-1",
                    "eventType": "outOfOffice",
                    "status": "confirmed",
                    "start": {"date": "2026-09-07"},
                    "end": {"date": "2026-09-09"},
                    "summary": "Private holiday detail",
                    "description": "Private destination",
                },
                {"id": "cancelled", "status": "cancelled"},
            ]
        },
    )
    source, store, factory = build_source(tmp_path, [first, second])

    calendar = await source.fetch()

    assert factory.client is not None
    calls = factory.client.calls
    assert len(calls) == 2
    assert calls[0][0].endswith("/team%2Fcalendar%40example.com/events")
    assert calls[0][1] is not None
    assert calls[0][1]["timeMin"] == "2026-08-06T12:00:00Z"
    assert calls[0][1]["timeMax"] == "2026-10-05T12:00:00Z"
    assert calls[1][1] is not None and calls[1][1]["pageToken"] == "next-page"
    events = {str(event["UID"]): event for event in _walk_components(calendar, "VEVENT")}
    timed = events["timed@example.com"]
    assert event_time(timed, "DTSTART").isoformat() == "2026-09-06T09:00:00+02:00"
    assert event_time(timed, "RECURRENCE-ID").isoformat() == "2026-09-06T08:00:00+02:00"
    assert str(timed["STATUS"]) == "TENTATIVE"
    assert str(timed["TRANSP"]) == "TRANSPARENT"
    assert "ATTENDEE" not in timed and "ORGANIZER" not in timed
    out_of_office = next(
        event for event in events.values() if "X-ICS-MERGER-ORIGINAL-STATUS" in event
    )
    assert event_time(out_of_office, "DTSTART").isoformat() == "2026-09-07"
    assert event_time(out_of_office, "DTEND").isoformat() == "2026-09-09"
    assert str(out_of_office["SUMMARY"]) == "Out of office"
    assert "Private" not in out_of_office.to_ical().decode()
    assert store.get("google") == {
        "access_token": "rotated-access",
        "refresh_token": "rotated-refresh",
    }


@pytest.mark.asyncio
async def test_google_missing_token_is_auth_required(tmp_path: Path) -> None:
    settings = Settings(
        google_client_id="client",
        google_client_secret=SecretStr("secret"),
    )
    config = provider_config(settings, "google")
    assert config is not None
    factory = ScriptedFactory([])
    source = GoogleCalendarSource(
        "primary", 30, TokenStore(tmp_path / "tokens.json"), config, factory
    )

    with pytest.raises(SourceError, match=SourceErrorCode.AUTH_REQUIRED):
        await source.fetch()
    assert factory.client is None


@pytest.mark.asyncio
async def test_google_invalid_grant_invalidates_token_and_does_not_abort_startup(
    tmp_path: Path,
) -> None:
    source, store, factory = build_source(tmp_path, [])
    factory.failure = OAuthClientError(reauthorization_required=True)

    with pytest.raises(SourceError, match=SourceErrorCode.AUTH_REQUIRED):
        await source.fetch()

    assert store.get("google") is None
    merger = CalendarMerger()
    await merger.start([source])
    await merger.stop()
