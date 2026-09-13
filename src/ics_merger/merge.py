import asyncio
import copy
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Protocol, cast

from icalendar import Calendar, Event
from icalendar.cal import Component
from icalendar.timezone import tzp

from ics_merger.sources.base import CalendarSource
from ics_merger.sources.errors import SourceError

logger = logging.getLogger(__name__)


class _PropertyWriter(Protocol):
    def add(self, name: str, value: object) -> None: ...


class _ComponentWalker(Protocol):
    def walk(self, name: str | None = None) -> list[Component]: ...


class _TimezoneProvider(Protocol):
    def timezone(self, timezone_id: str) -> tzinfo | None: ...


_TIMEZONE_PROVIDER = cast(_TimezoneProvider, tzp)


class AllSourcesFailedError(Exception):
    """Raised when no configured source produced a usable calendar."""


class SourceLabel(StrEnum):
    NONE = "none"
    TITLE = "title"
    DESCRIPTION = "description"


@dataclass(frozen=True, slots=True)
class MergeResult:
    content: bytes
    source_count: int
    failed_source_count: int


@dataclass(frozen=True, slots=True)
class _FetchedCalendar:
    source_name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _SourceState:
    fetched: _FetchedCalendar | None
    failed: bool


@dataclass(frozen=True, slots=True)
class _RenderedSnapshot:
    revision: int
    generated_at: datetime
    expires_at: datetime | None
    result: MergeResult


class CalendarMerger:
    """Aggregate parsed source calendars without provider-specific transformations."""

    def __init__(
        self,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        *,
        include_free_time: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._now = now
        self._include_free_time = include_free_time
        self._sleep = sleep
        self._source_states: list[_SourceState] | None = None
        self._refresh_tasks: list[asyncio.Task[None]] = []
        self._revision = 0
        self._render_cache: dict[tuple[SourceLabel, bool], _RenderedSnapshot] = {}

    async def start(self, sources: Sequence[CalendarSource]) -> None:
        if self._source_states is not None:
            raise RuntimeError("Calendar merger is already running")
        outcomes = await asyncio.gather(*(_fetch_source(source) for source in sources))
        self._source_states = [
            _SourceState(fetched=outcome, failed=outcome is None) for outcome in outcomes
        ]
        self._revision += 1
        self._render_cache.clear()
        self._refresh_tasks = [
            asyncio.create_task(
                self._refresh_periodically(index, source),
                name=f"refresh-calendar-{source.name}",
            )
            for index, source in enumerate(sources)
        ]

    async def stop(self) -> None:
        tasks, self._refresh_tasks = self._refresh_tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._source_states = None
        self._render_cache.clear()

    async def merge(
        self,
        sources: Sequence[CalendarSource] | None = None,
        source_label: SourceLabel = SourceLabel.NONE,
        *,
        include_history: bool = False,
    ) -> MergeResult:
        cache_key = (source_label, include_history)
        if sources is None:
            if self._source_states is None:
                raise RuntimeError("Calendar merger has not been started")
            states = tuple(self._source_states)
            current = self._now()
            cached = self._render_cache.get(cache_key)
            if (
                cached is not None
                and cached.revision == self._revision
                and cached.generated_at <= current
                and (cached.expires_at is None or current < cached.expires_at)
            ):
                return cached.result
        else:
            outcomes = await asyncio.gather(*(_fetch_source(source) for source in sources))
            states = tuple(
                _SourceState(fetched=outcome, failed=outcome is None) for outcome in outcomes
            )
            current = self._now()
        calendars = [state.fetched for state in states if state.fetched is not None]
        failed_count = sum(state.failed for state in states)
        if not calendars:
            logger.warning("all calendar sources failed", extra={"source_count": len(states)})
            raise AllSourcesFailedError
        if failed_count:
            logger.warning(
                "calendar merge completed with source failures",
                extra={"source_count": len(states), "failed_source_count": failed_count},
            )

        merged = Calendar()
        _add_property(merged, "prodid", "-//ICS Merger//EN")
        _add_property(merged, "version", "2.0")
        _add_property(merged, "calscale", "GREGORIAN")

        components: list[Component] = []
        for fetched in calendars:
            source_calendar = Calendar.from_ical(fetched.content)
            components.extend(_walk_components(source_calendar, "VTIMEZONE"))
            for source_event in _walk_components(source_calendar, "VEVENT"):
                event = copy.deepcopy(source_event)
                _namespace_uid(event, fetched.source_name)
                _add_source_label(event, fetched.source_name, source_label)
                components.append(event)
        active_components = _remove_ended_events(components, current)
        expires_at = _next_expiry(active_components, current)
        retained_components = components if include_history else active_components
        resolved_components = _resolve_availability(
            retained_components, self._include_free_time
        )
        if not include_history:
            resolved_components = _remove_ended_events(resolved_components, current)
        resolved_components.sort(key=_component_sort_key)
        for component in resolved_components:
            merged.add_component(component)

        result = MergeResult(
            content=merged.to_ical(),
            source_count=len(states),
            failed_source_count=failed_count,
        )
        if sources is None:
            self._render_cache[cache_key] = _RenderedSnapshot(
                revision=self._revision,
                generated_at=current,
                expires_at=expires_at,
                result=result,
            )
        return result

    async def _refresh_periodically(self, index: int, source: CalendarSource) -> None:
        while True:
            await self._sleep(source.ttl_seconds)
            try:
                fetched = await _fetch_source(source)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "unexpected calendar refresh failure",
                    extra={"source_name": source.name},
                )
                fetched = None
            assert self._source_states is not None
            previous = self._source_states[index]
            self._source_states[index] = _SourceState(
                fetched=fetched or previous.fetched,
                failed=fetched is None,
            )
            self._revision += 1
            self._render_cache.clear()


async def _fetch_source(source: CalendarSource) -> _FetchedCalendar | None:
    try:
        calendar = await source.fetch()
        return _FetchedCalendar(source.name, calendar.to_ical())
    except SourceError:
        return None


def _namespace_uid(component: Component, source_name: str) -> None:
    original = component.get("UID")
    identity = str(original).encode() if original is not None else component.to_ical()
    namespaced = hashlib.sha256(source_name.encode() + b"\0" + identity).hexdigest()
    if original is not None:
        del component["UID"]
    _add_property(component, "uid", f"{namespaced}@ics-merger.local")


def _add_source_label(component: Component, source_name: str, source_label: SourceLabel) -> None:
    if source_label is SourceLabel.TITLE:
        summary = component.get("SUMMARY")
        if summary is not None:
            del component["SUMMARY"]
        _add_property(component, "summary", f"[{source_name}] {summary or 'Busy'}")
    elif source_label is SourceLabel.DESCRIPTION:
        description = component.get("DESCRIPTION")
        if description is not None:
            del component["DESCRIPTION"]
        suffix = f"\n{description}" if description is not None else ""
        _add_property(component, "description", f"Calendar: {source_name}{suffix}")


def _component_sort_key(component: Component) -> tuple[int, bytes]:
    component_order = 0 if component.name == "VTIMEZONE" else 1
    return component_order, component.to_ical()


def _remove_ended_events(components: list[Component], now: datetime) -> list[Component]:
    timezone = _timeline_timezone(components)
    current = _as_datetime(now, timezone)
    return [component for component in components if not _has_ended(component, current)]


def _has_ended(component: Component, now: datetime) -> bool:
    if component.name != "VEVENT" or "RRULE" in component or "RDATE" in component:
        return False
    interval = _event_interval(component)
    if interval is None:
        return False
    _, end = interval
    return _as_datetime(end, now.tzinfo or UTC) <= now


def _next_expiry(components: list[Component], now: datetime) -> datetime | None:
    timezone = _timeline_timezone(components)
    current = _as_datetime(now, timezone)
    expirations: list[datetime] = []
    for component in components:
        if component.name != "VEVENT" or "RRULE" in component or "RDATE" in component:
            continue
        interval = _event_interval(component)
        if interval is None:
            continue
        expiration = _as_datetime(interval[1], timezone)
        if expiration > current:
            expirations.append(expiration)
    return min(expirations, default=None)


def _resolve_availability(components: list[Component], include_free_time: bool) -> list[Component]:
    blockers = [component for component in components if _is_blocker(component)]
    resolved = [component for component in components if not _is_free(component)]
    if include_free_time:
        resolved.extend(_free_time_events(blockers))
    return resolved


def _is_free(component: Component) -> bool:
    if component.name != "VEVENT":
        return False
    if _property_text(component, "STATUS") == "TENTATIVE":
        return False
    availability = _property_text(component, "X-ICS-MERGER-AVAILABILITY")
    if availability is not None:
        return availability in {
            "FREE",
            "TRANSPARENT",
            "OOF",
            "OUTOFOFFICE",
            "OUT-OF-OFFICE",
            "WORKINGELSEWHERE",
        }
    return _property_text(component, "TRANSP") == "TRANSPARENT"


def _is_blocker(component: Component) -> bool:
    return (
        component.name == "VEVENT"
        and _property_text(component, "STATUS") != "CANCELLED"
        and not _is_free(component)
    )


def _free_time_events(blockers: list[Component]) -> list[Component]:
    timezone = _timeline_timezone(blockers)
    intervals: list[tuple[datetime, datetime]] = []
    for blocker in blockers:
        interval = _event_interval(blocker)
        if interval is None:
            continue
        blocker_start, blocker_end = interval
        normalized = (
            _as_datetime(blocker_start, timezone),
            _as_datetime(blocker_end, timezone),
        )
        if normalized[1] > normalized[0]:
            intervals.append(normalized)

    coalesced: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if not coalesced or start > coalesced[-1][1]:
            coalesced.append((start, end))
            continue
        previous_start, previous_end = coalesced[-1]
        coalesced[-1] = (previous_start, max(previous_end, end))

    return [
        _free_time_event(previous_end, next_start)
        for (_, previous_end), (next_start, _) in zip(coalesced, coalesced[1:], strict=False)
        if previous_end < next_start
    ]


def _timeline_timezone(blockers: list[Component]) -> tzinfo:
    for blocker in blockers:
        interval = _event_interval(blocker)
        if interval is None:
            continue
        for value in interval:
            if isinstance(value, datetime) and value.tzinfo is not None:
                return _efficient_timezone(value.tzinfo)
    return UTC


def _as_datetime(value: date | datetime, timezone: tzinfo) -> datetime:
    timezone = _efficient_timezone(timezone)
    if not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    source_timezone = _efficient_timezone(value.tzinfo)
    normalized = value if source_timezone is value.tzinfo else value.replace(tzinfo=source_timezone)
    return normalized if source_timezone is timezone else normalized.astimezone(timezone)


def _efficient_timezone(timezone: tzinfo) -> tzinfo:
    timezone_id = getattr(timezone, "_tzid", None)
    if not isinstance(timezone_id, str):
        return timezone
    if timezone_id == "tzone://Microsoft/Utc":
        return UTC
    return _TIMEZONE_PROVIDER.timezone(timezone_id) or timezone


def _event_interval(component: Component) -> tuple[date | datetime, date | datetime] | None:
    if component.name != "VEVENT" or component.get("DTSTART") is None:
        return None
    start: object = component.decoded("DTSTART")
    if not isinstance(start, date):
        return None
    if component.get("DTEND") is None and component.get("DURATION") is not None:
        duration: object = component.decoded("DURATION")
        if not isinstance(duration, timedelta):
            return None
        end = start + duration
    elif component.get("DTEND") is None:
        end = start + (timedelta(0) if isinstance(start, datetime) else timedelta(days=1))
    else:
        end = component.decoded("DTEND")
    if not isinstance(end, date) or isinstance(start, datetime) != isinstance(end, datetime):
        return None
    return start, end


def _free_time_event(start: datetime, end: datetime) -> Component:
    event = Event()
    identity = f"free-time\0{start.isoformat()}\0{end.isoformat()}".encode()
    uid = f"{hashlib.sha256(identity).hexdigest()}@ics-merger.local"
    _add_property(event, "uid", uid)
    _add_property(event, "dtstamp", start.astimezone(UTC))
    _add_property(event, "dtstart", start)
    _add_property(event, "dtend", end)
    _add_property(event, "summary", "Free Time")
    _add_property(event, "status", "CONFIRMED")
    _add_property(event, "transp", "TRANSPARENT")
    _add_property(event, "X-ICS-MERGER-AVAILABILITY", "FREE")
    return event


def _property_text(component: Component, name: str) -> str | None:
    value = component.get(name)
    return str(value).upper() if value is not None else None


def _add_property(component: Component, name: str, value: object) -> None:
    cast(_PropertyWriter, component).add(name, value)


def _walk_components(component: Component, name: str | None = None) -> list[Component]:
    return cast(_ComponentWalker, component).walk(name)
