import ipaddress
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from icalendar import Calendar
from icalendar.cal import Component

from ics_merger.app import create_app
from ics_merger.config import RemoteCalendar, Settings, load_settings


class _ComponentWalker(Protocol):
    def walk(self, name: str | None = None) -> list[Component]: ...


def _walk_components(component: Component, name: str | None = None) -> list[Component]:
    return cast(_ComponentWalker, component).walk(name)


FIRST_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Conflicting Source Metadata//EN\r
X-WR-CALNAME:Private calendar name\r
BEGIN:VTIMEZONE\r
TZID:Europe/London\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:z-event\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260102T090000Z\r
DTEND:20260102T100000Z\r
SUMMARY:Later sort key\r
END:VEVENT\r
END:VCALENDAR\r
"""

SECOND_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Another Source//EN\r
BEGIN:VEVENT\r
UID:a-event\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260101T090000Z\r
DTEND:20260101T100000Z\r
SUMMARY:Earlier sort key\r
END:VEVENT\r
END:VCALENDAR\r
"""

PAST_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Past Source//EN\r
BEGIN:VEVENT\r
UID:past-event\r
DTSTAMP:20240101T000000Z\r
DTSTART:20241201T090000Z\r
DTEND:20241201T100000Z\r
SUMMARY:Past event\r
END:VEVENT\r
END:VCALENDAR\r
"""


async def public_resolver(
    hostname: str, port: int
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    del hostname, port
    return {ipaddress.ip_address("93.184.216.34")}


@asynccontextmanager
async def app_client(
    app: FastAPI, source_client: httpx.AsyncClient
) -> AsyncGenerator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client
    await source_client.aclose()


def build_app(
    handler: httpx.AsyncBaseTransport, urls: list[str]
) -> tuple[FastAPI, httpx.AsyncClient]:
    source_client = httpx.AsyncClient(transport=handler)
    settings = Settings(
        remote_ics_calendars=[
            RemoteCalendar(name=f"Calendar {index}", url=url)
            for index, url in enumerate(urls, start=1)
        ]
    )
    app = create_app(
        settings,
        source_client,
        public_resolver,
        now=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    return app, source_client


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(Settings(), source_client, public_resolver)
    async with app_client(app, source_client) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_root_serves_calendar_ui_and_old_routes_are_not_available() -> None:
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(Settings(), source_client, public_resolver)
    async with app_client(app, source_client) as client:
        response = await client.get("/")
        settings = await client.get("/settings")
        styles = await client.get("/assets/app.css")
        script = await client.get("/assets/app.js")
        settings_styles = await client.get("/assets/settings.css")
        settings_script = await client.get("/assets/settings.js")
        old_styles = await client.get("/api/assets/app.css")
        old_health = await client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="calendar"' in response.text
    assert 'href="/assets/app.css"' in response.text
    assert 'src="/assets/app.js"' in response.text
    assert 'href="/api/calendars/merged.ics"' in response.text
    assert 'href="/settings"' in response.text
    assert 'new URLSearchParams(window.location.search).get("source_label")' in script.text
    assert "state.page -= 1" in script.text
    assert "Math.max(0, state.page - 1)" not in script.text
    assert 'calendarUrl("/api/calendars/merged.json")' in script.text
    assert 'calendarUrl("/api/calendars/merged.ics")' in script.text
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert settings.status_code == 200
    assert 'id="settings-form"' in settings.text
    assert settings_styles.status_code == 200
    assert settings_script.status_code == 200
    assert old_styles.status_code == 404
    assert old_health.status_code == 404


@pytest.mark.asyncio
async def test_one_source_failure_is_isolated_and_ics_is_returned() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/failed.ics":
            return httpx.Response(502)
        return httpx.Response(200, headers={"content-type": "text/calendar"}, content=FIRST_ICS)

    app, source_client = build_app(
        httpx.MockTransport(handler),
        ["https://calendar.example/failed.ics", "https://calendar.example/ok.ics"],
    )
    async with app_client(app, source_client) as client:
        response = await client.get("/api/calendars/merged.ics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["x-ics-merger-sources"] == "2"
    assert response.headers["x-ics-merger-source-failures"] == "1"
    assert len(_walk_components(Calendar.from_ical(response.content), "VEVENT")) == 1


@pytest.mark.asyncio
async def test_all_source_failures_return_sanitized_service_error() -> None:
    handler = httpx.MockTransport(lambda _: httpx.Response(502))
    app, source_client = build_app(handler, ["https://calendar.example/private-token.ics"])
    async with app_client(app, source_client) as client:
        response = await client.get("/api/calendars/merged.ics")

    assert response.status_code == 503
    assert response.json() == {"detail": "No calendar source is currently available"}
    assert "private-token" not in response.text


@pytest.mark.asyncio
async def test_merged_ics_download_excludes_events_ended_by_the_present() -> None:
    source_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/calendar"},
                content=FIRST_ICS,
            )
        )
    )
    app = create_app(
        Settings(
            remote_ics_calendars=[
                RemoteCalendar(name="Calendar", url="https://calendar.example/calendar.ics")
            ]
        ),
        source_client,
        public_resolver,
        now=lambda: datetime(2026, 1, 2, 10, tzinfo=UTC),
    )

    async with app_client(app, source_client) as client:
        response = await client.get("/api/calendars/merged.ics")

    assert response.status_code == 200
    assert _walk_components(Calendar.from_ical(response.content), "VEVENT") == []


@pytest.mark.asyncio
async def test_merge_is_deterministic_and_ignores_source_calendar_metadata() -> None:
    upstream_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        content = FIRST_ICS if request.url.path == "/first.ics" else SECOND_ICS
        return httpx.Response(200, headers={"content-type": "text/calendar"}, content=content)

    urls = ["https://calendar.example/first.ics", "https://calendar.example/second.ics"]
    app, source_client = build_app(httpx.MockTransport(handler), urls)
    async with app_client(app, source_client) as client:
        first_response = await client.get("/api/calendars/merged.ics")
        second_response = await client.get("/api/calendars/merged.ics")

    assert first_response.content == second_response.content
    assert upstream_calls == 2
    merged = Calendar.from_ical(first_response.content)
    assert str(merged["PRODID"]) == "-//ICS Merger//EN"
    assert "X-WR-CALNAME" not in merged
    assert len(_walk_components(merged, "VTIMEZONE")) == 1
    events = _walk_components(merged, "VEVENT")
    source_uids = {str(event["UID"]) for event in events if str(event["SUMMARY"]) != "Free Time"}
    assert len(source_uids) == 2
    assert source_uids.isdisjoint({"a-event", "z-event"})
    assert [str(event["SUMMARY"]) for event in events].count("Free Time") == 1


@pytest.mark.asyncio
async def test_json_calendar_reports_sources_and_browser_events() -> None:
    app, source_client = build_app(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/calendar"}, content=PAST_ICS
            )
        ),
        ["https://calendar.example/calendar.ics"],
    )
    async with app_client(app, source_client) as client:
        response = await client.get("/api/calendars/merged.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sourceCount"] == 1
    assert payload["failedSourceCount"] == 0
    assert [event["title"] for event in payload["events"]] == ["Past event"]
    assert payload["generatedAt"].endswith("+00:00")


@pytest.mark.asyncio
async def test_source_label_is_applied_to_ics_and_json() -> None:
    app, source_client = build_app(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/calendar"}, content=FIRST_ICS
            )
        ),
        ["https://calendar.example/calendar.ics"],
    )
    async with app_client(app, source_client) as client:
        plain = await client.get("/api/calendars/merged.ics")
        titled = await client.get("/api/calendars/merged.ics?source_label=title")
        described = await client.get("/api/calendars/merged.json?source_label=description")

    plain_event = _walk_components(Calendar.from_ical(plain.content), "VEVENT")[0]
    titled_event = _walk_components(Calendar.from_ical(titled.content), "VEVENT")[0]
    assert str(plain_event["SUMMARY"]) == "Later sort key"
    assert str(titled_event["SUMMARY"]) == "[Calendar 1] Later sort key"
    assert described.json()["events"][0]["description"] == "Calendar: Calendar 1"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/calendars/merged.ics", "/api/calendars/merged.json"])
async def test_invalid_source_label_is_rejected(path: str) -> None:
    app, source_client = build_app(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/calendar"}, content=FIRST_ICS
            )
        ),
        ["https://calendar.example/calendar.ics"],
    )
    async with app_client(app, source_client) as client:
        response = await client.get(f"{path}?source_label=invalid")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_config_api_saves_and_applies_valid_changes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "server:\n  host: 127.0.0.1\n  port: 8000\n"
        "calendar:\n  future_horizon_days: 30\n"
        "remote_ics:\n  calendars: []\n"
        "storage:\n  token_file_path: data/tokens.json\n",
        encoding="utf-8",
    )
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(
        load_settings(config_path), source_client, public_resolver, config_path=config_path
    )
    async with app_client(app, source_client) as client:
        current = await client.get("/api/config")
        payload = current.json()["config"]
        payload["calendar"]["future_horizon_days"] = 120
        payload["calendar"]["include_free_time"] = False
        payload["server"]["port"] = 9000
        updated = await client.put(
            "/api/config",
            headers={"If-Match": current.json()["revision"]},
            json=payload,
        )

    assert current.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["restartRequired"] is True
    assert updated.json()["config"]["calendar"]["future_horizon_days"] == 120
    assert updated.json()["config"]["calendar"]["include_free_time"] is False
    assert load_settings(config_path).future_horizon_days == 120
    assert not load_settings(config_path).include_free_time
    assert app.state.runtime.settings.future_horizon_days == 120
    assert not app.state.runtime.settings.include_free_time


@pytest.mark.asyncio
async def test_config_api_rejects_stale_revision(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar:\n  future_horizon_days: 30\n", encoding="utf-8")
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(
        load_settings(config_path), source_client, public_resolver, config_path=config_path
    )
    async with app_client(app, source_client) as client:
        current = await client.get("/api/config")
        response = await client.put(
            "/api/config",
            headers={"If-Match": "stale"},
            json=current.json()["config"],
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Configuration changed; reload and retry"}


@pytest.mark.asyncio
async def test_config_api_is_unavailable_without_config_path() -> None:
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(Settings(), source_client, public_resolver)
    async with app_client(app, source_client) as client:
        response = await client.get("/api/config")

    assert response.status_code == 503
    assert response.json() == {"detail": "Configuration editing is unavailable"}


@pytest.mark.asyncio
async def test_config_api_replaces_live_calendar_sources(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "remote_ics:\n  calendars:\n"
        "    - name: First\n      url: https://calendar.example/first.ics\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        content = FIRST_ICS if request.url.path == "/first.ics" else SECOND_ICS
        return httpx.Response(200, headers={"content-type": "text/calendar"}, content=content)

    source_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(
        load_settings(config_path),
        source_client,
        public_resolver,
        now=lambda: datetime(2025, 1, 1, tzinfo=UTC),
        config_path=config_path,
    )
    async with app_client(app, source_client) as client:
        current = await client.get("/api/config")
        payload = current.json()["config"]
        payload["remote_ics"]["calendars"] = [
            {
                "name": "Second",
                "url": "https://calendar.example/second.ics",
                "ttl_seconds": 3600,
            }
        ]
        saved = await client.put(
            "/api/config",
            headers={"If-Match": current.json()["revision"]},
            json=payload,
        )
        merged = await client.get("/api/calendars/merged.ics")

    summaries = [
        str(event["SUMMARY"])
        for event in _walk_components(Calendar.from_ical(merged.content), "VEVENT")
        if str(event["SUMMARY"]) != "Free Time"
    ]
    assert saved.status_code == 200
    assert summaries == ["Earlier sort key"]
