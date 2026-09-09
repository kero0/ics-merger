import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx2
from icalendar import Calendar

from ics_merger.sources.errors import SourceError, SourceErrorCode

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
AddressResolver = Callable[[str, int], Awaitable[set[IpAddress]]]

_ALLOWED_CONTENT_TYPES = {
    "application/ics",
    "application/octet-stream",
    "text/calendar",
    "text/plain",
}


@dataclass(frozen=True, slots=True)
class RemoteIcsPolicy:
    max_response_bytes: int = 5 * 1024 * 1024
    allow_private_networks: bool = False
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    write_timeout_seconds: float = 5.0
    pool_timeout_seconds: float = 5.0


async def resolve_addresses(hostname: str, port: int) -> set[IpAddress]:
    loop = asyncio.get_running_loop()
    try:
        results = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError(SourceErrorCode.FETCH_FAILED) from exc
    return {ipaddress.ip_address(result[4][0]) for result in results}


def _validate_addresses(
    addresses: set[IpAddress],
    *,
    allow_private_networks: bool,
) -> None:
    if not addresses:
        raise SourceError(SourceErrorCode.FETCH_FAILED)
    if not allow_private_networks and any(not address.is_global for address in addresses):
        raise SourceError(SourceErrorCode.BLOCKED_ADDRESS)


class RemoteIcsAdapter:
    """Fetch and parse one untrusted remote ICS source."""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        policy: RemoteIcsPolicy,
        resolver: AddressResolver = resolve_addresses,
    ) -> None:
        self._client = client
        self._policy = policy
        self._resolver = resolver
        self._timeout = httpx2.Timeout(
            connect=policy.connect_timeout_seconds,
            read=policy.read_timeout_seconds,
            write=policy.write_timeout_seconds,
            pool=policy.pool_timeout_seconds,
        )

    async def fetch(self, source_url: str) -> Calendar:
        try:
            url = httpx2.URL(source_url)
        except httpx2.InvalidURL as exc:
            raise SourceError(SourceErrorCode.INVALID_URL) from exc
        if url.scheme not in {"http", "https"} or not url.host:
            raise SourceError(SourceErrorCode.INVALID_URL)

        port = url.port or (443 if url.scheme == "https" else 80)
        addresses = await self._resolver(url.host, port)
        _validate_addresses(
            addresses,
            allow_private_networks=self._policy.allow_private_networks,
        )
        request_url = url.copy_with(host=str(min(addresses, key=_address_sort_key)))
        headers = {
            "Accept": "text/calendar, application/ics, text/plain;q=0.5",
            "Host": _host_header(url),
        }

        try:
            async with self._client.stream(
                "GET",
                request_url,
                headers=headers,
                follow_redirects=False,
                timeout=self._timeout,
                extensions={"sni_hostname": url.host},
            ) as response:
                if response.is_redirect:
                    raise SourceError(SourceErrorCode.REDIRECT_REJECTED)
                if not response.is_success:
                    raise SourceError(SourceErrorCode.FETCH_FAILED)
                self._validate_content_type(response.headers.get("content-type"))
                body = await self._read_limited(response)
        except SourceError:
            raise
        except httpx2.HTTPError as exc:
            raise SourceError(SourceErrorCode.FETCH_FAILED) from exc

        try:
            calendar = Calendar.from_ical(body)
        except Exception as exc:
            raise SourceError(SourceErrorCode.MALFORMED_CALENDAR) from exc
        if calendar.name != "VCALENDAR":
            raise SourceError(SourceErrorCode.MALFORMED_CALENDAR)
        return calendar

    def _validate_content_type(self, content_type_header: str | None) -> None:
        if content_type_header is None:
            return
        media_type = content_type_header.partition(";")[0].strip().lower()
        if media_type not in _ALLOWED_CONTENT_TYPES:
            raise SourceError(SourceErrorCode.INVALID_CONTENT_TYPE)

    async def _read_limited(self, response: httpx2.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._policy.max_response_bytes:
                    raise SourceError(SourceErrorCode.RESPONSE_TOO_LARGE)
            except ValueError:
                pass

        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._policy.max_response_bytes:
                raise SourceError(SourceErrorCode.RESPONSE_TOO_LARGE)
        return bytes(body)


def _address_sort_key(address: IpAddress) -> tuple[int, int]:
    return address.version, int(address)


def _host_header(url: httpx2.URL) -> str:
    host = f"[{url.host}]" if ":" in url.host else url.host
    default_port = 443 if url.scheme == "https" else 80
    return host if url.port in {None, default_port} else f"{host}:{url.port}"


@dataclass(frozen=True, slots=True)
class RemoteIcsSource:
    adapter: RemoteIcsAdapter
    url: str
    name: str = "Remote"
    ttl_seconds: float = 3600.0

    async def fetch(self) -> Calendar:
        return await self.adapter.fetch(self.url)
