import asyncio
import ipaddress
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, cast

import httpx2 as httpx
import pytest
import vobject
from fastapi import FastAPI
from icalendar import Calendar, Event
from icalendar.cal import Component
from pydantic import SecretStr

from ics_merger.app import create_app
from ics_merger.config import RemoteCalendar, Settings
from ics_merger.merge import AllSourcesFailedError, CalendarMerger, SourceLabel
from ics_merger.oauth import OAuthProviderConfig, TokenUpdate
from ics_merger.oauth_state import OAuthTransaction
from ics_merger.sources.errors import SourceError, SourceErrorCode
from ics_merger.token_store import Token, TokenStore

REMOTE_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Remote//EN\r
BEGIN:VEVENT\r
UID:remote-event\r
DTSTAMP:20260901T000000Z\r
DTSTART:20260906T100000Z\r
DTEND:20260906T110000Z\r
END:VEVENT\r
END:VCALENDAR\r
"""


class _VObjectCalendar(Protocol):
    vevent_list: list[object]


class _PropertyWriter(Protocol):
    def add(self, name: str, value: object) -> None: ...


class _ComponentWalker(Protocol):
    def walk(self, name: str | None = None) -> list[Component]: ...


class _VObjectReader(Protocol):
    def readOne(self, source: str) -> _VObjectCalendar: ...


_VOBJECT_READER = cast(_VObjectReader, vobject)


def _add_property(component: Component, name: str, value: object) -> None:
    cast(_PropertyWriter, component).add(name, value)


def _walk_components(component: Component, name: str | None = None) -> list[Component]:
    return cast(_ComponentWalker, component).walk(name)


class ProviderClient:
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
        del url, params, headers
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "google-event",
                        "start": {"dateTime": "2026-09-06T12:00:00Z"},
                        "end": {"dateTime": "2026-09-06T13:00:00Z"},
                    }
                ]
            },
        )

    async def close(self) -> None:
        pass


class ProviderFactory:
    def __call__(
        self,
        config: OAuthProviderConfig,
        token: Token | None = None,
        token_update: TokenUpdate | None = None,
    ) -> ProviderClient:
        del config, token, token_update
        return ProviderClient()


async def public_resolver(
    hostname: str, port: int
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    del hostname, port
    return {ipaddress.ip_address("93.184.216.34")}


@asynccontextmanager
async def app_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_remote_and_google_sources_merge_together(tmp_path: Path) -> None:
    token_path = tmp_path / "tokens.json"
    TokenStore(token_path).set("google", {"access_token": "access"})
    settings = Settings(
        remote_ics_calendars=[
            RemoteCalendar(name="Remote", url="https://calendar.example/feed.ics")
        ],
        google_client_id="client",
        google_client_secret=SecretStr("secret"),
        token_file_path=token_path,
    )
    source_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/calendar"}, content=REMOTE_ICS
            )
        )
    )
    app = create_app(
        settings,
        source_client,
        public_resolver,
        ProviderFactory(),
        now=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )

    async with app_client(app) as client:
        response = await client.get("/api/calendars/merged.ics")
    await source_client.aclose()

    assert response.status_code == 200
    assert response.headers["x-ics-merger-sources"] == "2"
    assert response.headers["x-ics-merger-source-failures"] == "0"
    events = _walk_components(Calendar.from_ical(response.content), "VEVENT")
    assert len(events) == 3
    assert [str(event.get("SUMMARY")) for event in events].count("Free Time") == 1
    assert len(_VOBJECT_READER.readOne(response.text).vevent_list) == 3


@pytest.mark.asyncio
async def test_provider_without_token_is_an_all_sources_typed_failure(tmp_path: Path) -> None:
    settings = Settings(
        google_client_id="client",
        google_client_secret=SecretStr("secret"),
        token_file_path=tmp_path / "missing.json",
    )
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(settings, source_client, oauth_client_factory=ProviderFactory())

    async with app_client(app) as client:
        response = await client.get("/api/calendars/merged.ics")
    await source_client.aclose()

    assert response.status_code == 503
    assert response.json() == {"detail": "No calendar source is currently available"}


class FailingSource:
    name = "Failing"
    ttl_seconds = 3600.0

    async def fetch(self) -> Calendar:
        raise SourceError(SourceErrorCode.PROVIDER_FETCH_FAILED)


class BrokenSource:
    name = "Broken"
    ttl_seconds = 3600.0

    async def fetch(self) -> Calendar:
        raise RuntimeError("programming defect")


class StaticSource:
    def __init__(self, calendar: Calendar, name: str = "Test", ttl_seconds: float = 3600.0) -> None:
        self.name = name
        self.ttl_seconds = ttl_seconds
        self._calendar = calendar

    async def fetch(self) -> Calendar:
        return self._calendar


class CountingSource(StaticSource):
    def __init__(self, calendar: Calendar, name: str = "Test", ttl_seconds: float = 3600.0) -> None:
        super().__init__(calendar, name, ttl_seconds)
        self.calls = 0
        self.refresh_started = asyncio.Event()
        self.release_refresh = asyncio.Event()

    async def fetch(self) -> Calendar:
        self.calls += 1
        if self.calls > 1:
            self.refresh_started.set()
            await self.release_refresh.wait()
        return await super().fetch()


class FailsOnRefreshSource(StaticSource):
    def __init__(self, calendar: Calendar, ttl_seconds: float) -> None:
        super().__init__(calendar, ttl_seconds=ttl_seconds)
        self.calls = 0
        self.refresh_attempted = asyncio.Event()

    async def fetch(self) -> Calendar:
        self.calls += 1
        if self.calls > 1:
            self.refresh_attempted.set()
            raise SourceError(SourceErrorCode.PROVIDER_FETCH_FAILED)
        return await super().fetch()


class ControlledSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) == 1:
            self.started.set()
            await self.release.wait()
        else:
            await asyncio.Future()


@pytest.mark.asyncio
async def test_all_typed_failures_are_isolated() -> None:
    with pytest.raises(AllSourcesFailedError):
        await CalendarMerger().merge([FailingSource(), FailingSource()])


@pytest.mark.asyncio
async def test_unexpected_source_errors_are_not_silenced() -> None:
    with pytest.raises(RuntimeError, match="programming defect"):
        await CalendarMerger().merge([FailingSource(), BrokenSource()])


@pytest.mark.asyncio
async def test_source_free_events_are_replaced_with_gaps_between_blockers() -> None:
    calendar = Calendar()
    _add_property(calendar, "version", "2.0")
    calendar.add_component(
        _event("free", date(2026, 9, 3), date(2026, 9, 10), transparency="TRANSPARENT")
    )
    calendar.add_component(
        _event(
            "busy",
            datetime(2026, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 9, 5, 11, tzinfo=UTC),
        )
    )
    calendar.add_component(_event("away", date(2026, 9, 7), date(2026, 9, 8)))
    calendar.add_component(
        _event(
            "tentative",
            datetime(2026, 9, 9, 9, tzinfo=UTC),
            datetime(2026, 9, 9, 10, tzinfo=UTC),
            transparency="TRANSPARENT",
            status="TENTATIVE",
        )
    )

    result = await CalendarMerger(now=lambda: datetime(2026, 9, 5, 9, tzinfo=UTC)).merge(
        [StaticSource(calendar)]
    )

    merged = Calendar.from_ical(result.content)
    events = _walk_components(merged, "VEVENT")
    free_events = [event for event in events if str(event["SUMMARY"]) == "Free Time"]
    assert [(_temporal(event, "DTSTART"), _temporal(event, "DTEND")) for event in free_events] == [
        (
            datetime(2026, 9, 5, 11, tzinfo=UTC),
            datetime(2026, 9, 7, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 8, tzinfo=UTC),
            datetime(2026, 9, 9, 9, tzinfo=UTC),
        ),
    ]
    source_uids = {str(event["UID"]) for event in events if str(event["SUMMARY"]) != "Free Time"}
    assert len(source_uids) == 3
    assert source_uids.isdisjoint({"away", "busy", "tentative"})
    assert "free" not in {str(event["UID"]) for event in events}
    assert len({str(event["UID"]) for event in free_events}) == 2
    assert len(_VOBJECT_READER.readOne(result.content.decode()).vevent_list) == 5


@pytest.mark.asyncio
async def test_free_time_insertion_can_be_disabled() -> None:
    calendar = Calendar()
    _add_property(calendar, "version", "2.0")
    calendar.add_component(
        _event(
            "free",
            datetime(2026, 9, 5, 9, tzinfo=UTC),
            datetime(2026, 9, 5, 17, tzinfo=UTC),
            transparency="TRANSPARENT",
        )
    )
    calendar.add_component(
        _event(
            "morning",
            datetime(2026, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 9, 5, 11, tzinfo=UTC),
        )
    )
    calendar.add_component(
        _event(
            "afternoon",
            datetime(2026, 9, 5, 14, tzinfo=UTC),
            datetime(2026, 9, 5, 15, tzinfo=UTC),
        )
    )

    result = await CalendarMerger(
        now=lambda: datetime(2026, 9, 5, 8, tzinfo=UTC),
        include_free_time=False,
    ).merge([StaticSource(calendar)])

    events = _walk_components(Calendar.from_ical(result.content), "VEVENT")
    assert len(events) == 2
    assert "Free Time" not in {str(event.get("SUMMARY")) for event in events}
    assert "free" not in {str(event["UID"]) for event in events}


@pytest.mark.asyncio
async def test_free_time_is_created_between_coalesced_blocking_events() -> None:
    calendar = Calendar()
    _add_property(calendar, "version", "2.0")
    calendar.add_component(
        _event(
            "free",
            datetime(2026, 9, 5, 9, tzinfo=UTC),
            datetime(2026, 9, 5, 17, tzinfo=UTC),
            transparency="TRANSPARENT",
        )
    )
    for uid, start, end in (
        ("before", 8, 9),
        ("busy", 10, 12),
        ("nested", 11, 12),
        ("adjacent", 12, 13),
        ("after", 17, 18),
    ):
        calendar.add_component(
            _event(
                uid,
                datetime(2026, 9, 5, start, tzinfo=UTC),
                datetime(2026, 9, 5, end, tzinfo=UTC),
            )
        )

    merger = CalendarMerger(now=lambda: datetime(2026, 9, 5, 7, tzinfo=UTC))
    first = await merger.merge([StaticSource(calendar)])
    second = await merger.merge([StaticSource(calendar)])

    merged = Calendar.from_ical(first.content)
    events = _walk_components(merged, "VEVENT")
    free_events = [event for event in events if str(event["SUMMARY"]) == "Free Time"]
    assert [(_temporal(event, "DTSTART"), _temporal(event, "DTEND")) for event in free_events] == [
        (
            datetime(2026, 9, 5, 9, tzinfo=UTC),
            datetime(2026, 9, 5, 10, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 5, 13, tzinfo=UTC),
            datetime(2026, 9, 5, 17, tzinfo=UTC),
        ),
    ]
    assert "free" not in {str(event["UID"]) for event in events}
    assert len({str(event["UID"]) for event in free_events}) == 2
    assert first.content == second.content


@pytest.mark.asyncio
async def test_events_ended_by_the_present_are_removed_before_merging() -> None:
    calendar = Calendar()
    _add_property(calendar, "version", "2.0")
    calendar.add_component(
        _event(
            "expired",
            datetime(2026, 9, 5, 8, tzinfo=UTC),
            datetime(2026, 9, 5, 9, tzinfo=UTC),
        )
    )
    calendar.add_component(
        _event(
            "ends-now",
            datetime(2026, 9, 5, 11, tzinfo=UTC),
            datetime(2026, 9, 5, 12, tzinfo=UTC),
        )
    )
    calendar.add_component(
        _event(
            "ongoing",
            datetime(2026, 9, 5, 11, 30, tzinfo=UTC),
            datetime(2026, 9, 5, 13, tzinfo=UTC),
        )
    )
    calendar.add_component(_event("past-day", date(2026, 9, 4), date(2026, 9, 5)))
    calendar.add_component(_event("today", date(2026, 9, 5), date(2026, 9, 6)))
    recurring = _event(
        "recurring",
        datetime(2026, 1, 1, 9, tzinfo=UTC),
        datetime(2026, 1, 1, 10, tzinfo=UTC),
    )
    _add_property(recurring, "rrule", {"freq": "weekly"})
    calendar.add_component(recurring)

    result = await CalendarMerger(now=lambda: datetime(2026, 9, 5, 12, tzinfo=UTC)).merge(
        [StaticSource(calendar)]
    )

    events = _walk_components(Calendar.from_ical(result.content), "VEVENT")
    source_uids = {
        str(event["UID"]) for event in events if str(event.get("SUMMARY")) != "Free Time"
    }
    assert len(source_uids) == 3
    assert source_uids.isdisjoint({"ongoing", "recurring", "today"})


@pytest.mark.asyncio
async def test_history_can_be_included_for_calendar_view() -> None:
    calendar = Calendar()
    _add_property(calendar, "version", "2.0")
    calendar.add_component(
        _event(
            "expired",
            datetime(2026, 9, 4, 8, tzinfo=UTC),
            datetime(2026, 9, 4, 9, tzinfo=UTC),
        )
    )
    calendar.add_component(
        _event(
            "future",
            datetime(2026, 9, 6, 8, tzinfo=UTC),
            datetime(2026, 9, 6, 9, tzinfo=UTC),
        )
    )

    result = await CalendarMerger(
        now=lambda: datetime(2026, 9, 5, 12, tzinfo=UTC), include_free_time=False
    ).merge([StaticSource(calendar)], include_history=True)

    events = _walk_components(Calendar.from_ical(result.content), "VEVENT")
    assert {str(event["SUMMARY"]) for event in events} == {"expired", "future"}


@pytest.mark.asyncio
async def test_source_names_namespace_uids_and_optionally_label_events() -> None:
    first_calendar = Calendar()
    first_calendar.add_component(
        _event(
            "shared",
            datetime(2026, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 9, 5, 11, tzinfo=UTC),
        )
    )
    second_calendar = Calendar()
    second_event = _event(
        "shared",
        datetime(2026, 9, 5, 12, tzinfo=UTC),
        datetime(2026, 9, 5, 13, tzinfo=UTC),
    )
    _add_property(second_event, "description", "Existing notes")
    second_calendar.add_component(second_event)
    merger = CalendarMerger(now=lambda: datetime(2026, 9, 5, 9, tzinfo=UTC))
    sources = [StaticSource(first_calendar, "Work"), StaticSource(second_calendar, "Personal")]

    plain = _walk_components(Calendar.from_ical((await merger.merge(sources)).content), "VEVENT")
    titled = _walk_components(
        Calendar.from_ical((await merger.merge(sources, SourceLabel.TITLE)).content),
        "VEVENT",
    )
    described = _walk_components(
        Calendar.from_ical((await merger.merge(sources, SourceLabel.DESCRIPTION)).content),
        "VEVENT",
    )

    plain_source_events = [event for event in plain if str(event["SUMMARY"]) != "Free Time"]
    assert len({str(event["UID"]) for event in plain_source_events}) == 2
    assert {str(event["SUMMARY"]) for event in plain_source_events} == {"shared"}
    assert {str(event["SUMMARY"]) for event in titled} == {
        "[Personal] shared",
        "[Work] shared",
        "Free Time",
    }
    descriptions = {
        str(event["DESCRIPTION"]) for event in described if event.get("DESCRIPTION") is not None
    }
    assert descriptions == {"Calendar: Personal\nExisting notes", "Calendar: Work"}
    assert (
        next(event for event in described if str(event["SUMMARY"]) == "Free Time").get(
            "DESCRIPTION"
        )
        is None
    )


@pytest.mark.asyncio
async def test_sources_refresh_in_background_without_blocking_merges() -> None:
    calendar = Calendar()
    calendar.add_component(
        _event(
            "event",
            datetime(2026, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 9, 5, 11, tzinfo=UTC),
        )
    )
    source = CountingSource(calendar, "Work", ttl_seconds=120)
    controlled_sleep = ControlledSleep()
    merger = CalendarMerger(
        now=lambda: datetime(2026, 9, 5, 9, tzinfo=UTC),
        sleep=controlled_sleep,
    )

    await merger.start([source])
    try:
        await controlled_sleep.started.wait()
        titled = await merger.merge(source_label=SourceLabel.TITLE)
        described = await merger.merge(source_label=SourceLabel.DESCRIPTION)
        assert source.calls == 1
        assert controlled_sleep.delays == [120]
        assert "[Work] event" in titled.content.decode()
        assert "Calendar: Work" in described.content.decode()

        controlled_sleep.release.set()
        await source.refresh_started.wait()
        during_refresh = await merger.merge()
        assert source.calls == 2
        assert "event" in during_refresh.content.decode()
    finally:
        source.release_refresh.set()
        await merger.stop()


@pytest.mark.asyncio
async def test_failed_refresh_keeps_the_last_successful_calendar() -> None:
    calendar = Calendar()
    calendar.add_component(
        _event(
            "still-available",
            datetime(2026, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 9, 5, 11, tzinfo=UTC),
        )
    )
    source = FailsOnRefreshSource(calendar, ttl_seconds=60)
    controlled_sleep = ControlledSleep()
    merger = CalendarMerger(
        now=lambda: datetime(2026, 9, 5, 9, tzinfo=UTC),
        sleep=controlled_sleep,
    )

    await merger.start([source])
    try:
        await controlled_sleep.started.wait()
        controlled_sleep.release.set()
        await source.refresh_attempted.wait()
        result = None
        for _ in range(10):
            result = await merger.merge()
            if result.failed_source_count == 1:
                break
            await asyncio.sleep(0)

        assert result is not None
        assert result.failed_source_count == 1
        assert "still-available" in result.content.decode()
    finally:
        await merger.stop()


@pytest.mark.asyncio
async def test_render_cache_expires_when_an_event_ends() -> None:
    calendar = Calendar()
    calendar.add_component(
        _event(
            "event",
            datetime(2026, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 9, 5, 11, tzinfo=UTC),
        )
    )
    current = datetime(2026, 9, 5, 9, tzinfo=UTC)
    merger = CalendarMerger(now=lambda: current)

    await merger.start([StaticSource(calendar)])
    try:
        first = await merger.merge()
        second = await merger.merge()
        titled = await merger.merge(source_label=SourceLabel.TITLE)

        assert second is first
        assert titled is not first

        current = datetime(2026, 9, 5, 11, tzinfo=UTC)
        expired = await merger.merge()
        assert expired is not first
        assert _walk_components(Calendar.from_ical(expired.content), "VEVENT") == []
    finally:
        await merger.stop()


def _event(
    uid: str,
    start: date | datetime,
    end: date | datetime,
    *,
    transparency: str = "OPAQUE",
    status: str = "CONFIRMED",
) -> Event:
    event = Event()
    _add_property(event, "uid", uid)
    _add_property(event, "summary", uid)
    _add_property(event, "dtstart", start)
    _add_property(event, "dtend", end)
    _add_property(event, "transp", transparency)
    _add_property(event, "status", status)
    return event


def _temporal(component: Component, name: str) -> date | datetime:
    value: object = component.decoded(name)
    assert isinstance(value, date)
    return value
