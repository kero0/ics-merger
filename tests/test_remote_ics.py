import ipaddress
from typing import Protocol, cast

import httpx2 as httpx
import pytest
from icalendar.cal import Component

from ics_merger.sources.errors import SourceError, SourceErrorCode
from ics_merger.sources.remote_ics import RemoteIcsAdapter, RemoteIcsPolicy


class _ComponentWalker(Protocol):
    def walk(self, name: str | None = None) -> list[Component]: ...


def _walk_components(component: Component, name: str | None = None) -> list[Component]:
    return cast(_ComponentWalker, component).walk(name)


VALID_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Source//EN\r
BEGIN:VEVENT\r
UID:event-1\r
DTSTAMP:20260101T000000Z\r
DTSTART:20260102T000000Z\r
DTEND:20260102T010000Z\r
SUMMARY:Example\r
END:VEVENT\r
END:VCALENDAR\r
"""


async def public_resolver(
    hostname: str, port: int
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    assert hostname == "calendar.example"
    assert port == 443
    return {ipaddress.ip_address("93.184.216.34")}


@pytest.mark.asyncio
async def test_fetches_and_parses_calendar_with_injected_client() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"].startswith("text/calendar")
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "calendar.example"
        assert request.extensions["sni_hostname"] == "calendar.example"
        return httpx.Response(200, headers={"content-type": "text/calendar"}, content=VALID_ICS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RemoteIcsAdapter(client, RemoteIcsPolicy(), public_resolver)
        calendar = await adapter.fetch("https://calendar.example/feed.ics")

    events = _walk_components(calendar, "VEVENT")
    assert len(events) == 1
    assert str(events[0]["UID"]) == "event-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_url",
    ["ftp://calendar.example/feed.ics", "file:///tmp/feed.ics", "http://[::1"],
)
async def test_rejects_unsupported_url_schemes(source_url: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail())) as client:
        adapter = RemoteIcsAdapter(client, RemoteIcsPolicy(), public_resolver)
        with pytest.raises(SourceError, match=SourceErrorCode.INVALID_URL):
            await adapter.fetch(source_url)


@pytest.mark.asyncio
async def test_rejects_non_global_resolved_addresses() -> None:
    async def private_resolver(
        hostname: str, port: int
    ) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        del hostname, port
        return {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("10.0.0.1")}

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail())) as client:
        adapter = RemoteIcsAdapter(client, RemoteIcsPolicy(), private_resolver)
        with pytest.raises(SourceError, match=SourceErrorCode.BLOCKED_ADDRESS):
            await adapter.fetch("https://calendar.example/feed.ics")


@pytest.mark.asyncio
async def test_rejects_oversized_streamed_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "text/calendar"}, content=VALID_ICS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RemoteIcsAdapter(
            client,
            RemoteIcsPolicy(max_response_bytes=16),
            public_resolver,
        )
        with pytest.raises(SourceError, match=SourceErrorCode.RESPONSE_TOO_LARGE):
            await adapter.fetch("https://calendar.example/feed.ics")


@pytest.mark.asyncio
async def test_rejects_malformed_calendar_without_exposing_content() -> None:
    private_body = b"not a calendar: private title"

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "text/calendar"}, content=private_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RemoteIcsAdapter(client, RemoteIcsPolicy(), public_resolver)
        with pytest.raises(SourceError, match=SourceErrorCode.MALFORMED_CALENDAR) as error:
            await adapter.fetch("https://calendar.example/feed.ics?token=secret")

    assert "private title" not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_rejects_redirect_without_requesting_destination() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal request_count
        request_count += 1
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private.ics"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RemoteIcsAdapter(client, RemoteIcsPolicy(), public_resolver)
        with pytest.raises(SourceError, match=SourceErrorCode.REDIRECT_REJECTED):
            await adapter.fetch("https://calendar.example/feed.ics")

    assert request_count == 1


@pytest.mark.asyncio
async def test_rejects_explicit_non_calendar_content_type() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "text/html"}, content=VALID_ICS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RemoteIcsAdapter(client, RemoteIcsPolicy(), public_resolver)
        with pytest.raises(SourceError, match=SourceErrorCode.INVALID_CONTENT_TYPE):
            await adapter.fetch("https://calendar.example/feed.ics")


@pytest.mark.asyncio
async def test_private_network_access_requires_explicit_opt_in() -> None:
    async def private_resolver(
        hostname: str, port: int
    ) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        del hostname, port
        return {ipaddress.ip_address("10.0.0.1")}

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "text/calendar"}, content=VALID_ICS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RemoteIcsAdapter(
            client,
            RemoteIcsPolicy(allow_private_networks=True),
            private_resolver,
        )
        calendar = await adapter.fetch("https://calendar.example/feed.ics")

    assert len(_walk_components(calendar, "VEVENT")) == 1
