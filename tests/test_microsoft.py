from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, cast

import httpx2 as httpx
import pytest
from icalendar.cal import Component
from pydantic import SecretStr

from ics_merger.config import Settings
from ics_merger.oauth import OAuthProviderConfig, TokenUpdate, provider_config
from ics_merger.oauth_state import OAuthTransaction
from ics_merger.sources.errors import SourceError, SourceErrorCode
from ics_merger.sources.microsoft import MicrosoftCalendarSource
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
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, str] | None, Mapping[str, str] | None]] = []

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
        self.calls.append((url, params, headers))
        return self.responses.pop(0)

    async def close(self) -> None:
        pass


class ScriptedFactory:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.client = ScriptedClient(responses)

    def __call__(
        self,
        config: OAuthProviderConfig,
        token: Token | None = None,
        token_update: TokenUpdate | None = None,
    ) -> ScriptedClient:
        del config, token, token_update
        return self.client


def build_source(
    tmp_path: Path, responses: list[httpx.Response]
) -> tuple[MicrosoftCalendarSource, ScriptedFactory]:
    settings = Settings(
        microsoft_client_id="client",
        microsoft_client_secret=SecretStr("secret"),
        token_file_path=tmp_path / "tokens.json",
    )
    store = TokenStore(settings.token_file_path)
    store.set("microsoft", {"access_token": "access", "refresh_token": "refresh"})
    config = provider_config(settings, "microsoft")
    assert config is not None
    factory = ScriptedFactory(responses)
    source = MicrosoftCalendarSource(
        "primary",
        14,
        store,
        config,
        factory,
        now=lambda: datetime(2026, 9, 5, 12, tzinfo=UTC),
    )
    return source, factory


@pytest.mark.asyncio
async def test_microsoft_pagination_and_deterministic_conversion(tmp_path: Path) -> None:
    next_link = "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=safe"
    first = httpx.Response(
        200,
        json={
            "value": [
                {
                    "id": "event-1",
                    "iCalUId": "graph-event",
                    "subject": "Focus",
                    "bodyPreview": "Prepare",
                    "location": {"displayName": "Room 2"},
                    "start": {"dateTime": "2026-09-06T09:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-09-06T10:00:00", "timeZone": "UTC"},
                    "originalStart": "2026-09-06T08:00:00Z",
                    "lastModifiedDateTime": "2026-09-01T10:00:00Z",
                    "showAs": "tentative",
                    "isAllDay": False,
                    "isCancelled": False,
                    "attendees": [{"emailAddress": {"address": "private@example.com"}}],
                    "organizer": {"emailAddress": {"address": "private@example.com"}},
                }
            ],
            "@odata.nextLink": next_link,
        },
    )
    second = httpx.Response(
        200,
        json={
            "value": [
                {
                    "id": "ooo-1",
                    "subject": "Private leave reason",
                    "bodyPreview": "Private details",
                    "start": {"dateTime": "2026-09-07T00:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-09-09T00:00:00", "timeZone": "UTC"},
                    "showAs": "oof",
                    "isAllDay": True,
                    "isCancelled": False,
                },
                {"id": "cancelled", "isCancelled": True},
            ]
        },
    )
    source, factory = build_source(tmp_path, [first, second])

    calendar = await source.fetch()

    calls = factory.client.calls
    assert len(calls) == 2
    assert calls[0][0] == "https://graph.microsoft.com/v1.0/me/calendarView"
    assert calls[0][1] is not None
    assert calls[0][1]["startDateTime"] == "2026-08-22T12:00:00Z"
    assert calls[0][1]["endDateTime"] == "2026-09-19T12:00:00Z"
    assert calls[0][2] == {"Prefer": 'outlook.timezone="UTC"'}
    assert calls[1][0] == next_link and calls[1][1] is None
    events = {str(event["UID"]): event for event in _walk_components(calendar, "VEVENT")}
    timed = events["graph-event"]
    assert event_time(timed, "DTSTART") == datetime(2026, 9, 6, 9, tzinfo=UTC)
    assert str(timed["STATUS"]) == "TENTATIVE"
    assert str(timed["X-ICS-MERGER-AVAILABILITY"]) == "TENTATIVE"
    assert "ATTENDEE" not in timed and "ORGANIZER" not in timed
    out_of_office = next(
        event for event in events.values() if "X-ICS-MERGER-ORIGINAL-STATUS" in event
    )
    assert event_time(out_of_office, "DTEND").isoformat() == "2026-09-09"
    assert str(out_of_office["TRANSP"]) == "TRANSPARENT"
    assert "Private" not in out_of_office.to_ical().decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_link",
    [
        "http://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=x",
        "https://graph.microsoft.com.evil.example/v1.0/me/calendarView",
        "https://user@graph.microsoft.com/v1.0/me/calendarView",
        "https://graph.microsoft.com/beta/me/calendarView",
    ],
)
async def test_microsoft_rejects_untrusted_next_link(tmp_path: Path, next_link: str) -> None:
    source, factory = build_source(
        tmp_path,
        [httpx.Response(200, json={"value": [], "@odata.nextLink": next_link})],
    )

    with pytest.raises(SourceError, match=SourceErrorCode.INVALID_PAGINATION):
        await source.fetch()
    assert len(factory.client.calls) == 1
