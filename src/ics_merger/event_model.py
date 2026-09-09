import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol, cast

from icalendar import Calendar, Event
from icalendar.cal import Component


class _PropertyWriter(Protocol):
    def add(self, name: str, value: object) -> None: ...


def _add_property(component: Component, name: str, value: object) -> None:
    cast(_PropertyWriter, component).add(name, value)


class EventConversionError(Exception):
    """Provider data cannot be represented safely as an event."""


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    uid: str
    start: date | datetime
    end: date | datetime
    stamp: datetime
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    status: str = "CONFIRMED"
    transparency: str = "OPAQUE"
    availability: str | None = None
    out_of_office: bool = False
    recurrence_id: date | datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.start, datetime) != isinstance(self.end, datetime):
            raise EventConversionError
        for value in (self.start, self.end, self.stamp, self.recurrence_id):
            if isinstance(value, datetime) and value.tzinfo is None:
                raise EventConversionError


def provider_calendar(events: list[NormalizedEvent]) -> Calendar:
    calendar = Calendar()
    _add_property(calendar, "prodid", "-//ICS Merger Provider Adapter//EN")
    _add_property(calendar, "version", "2.0")
    _add_property(calendar, "calscale", "GREGORIAN")
    for normalized in sorted(events, key=lambda event: (event.uid, _temporal_key(event.start))):
        calendar.add_component(_to_vevent(normalized))
    return calendar


def stable_fallback_uid(
    provider: str,
    calendar_id: str,
    event_id: str,
    recurrence_identity: str = "",
) -> str:
    digest = hashlib.sha256(
        f"{provider}\0{calendar_id}\0{event_id}\0{recurrence_identity}".encode()
    ).hexdigest()
    return f"{digest}@ics-merger.local"


def parse_rfc3339(value: object, *, assume_utc: bool = False) -> datetime:
    if not isinstance(value, str) or not value:
        raise EventConversionError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventConversionError from exc
    if parsed.tzinfo is None:
        if not assume_utc:
            raise EventConversionError
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise EventConversionError
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise EventConversionError from exc


def text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _to_vevent(normalized: NormalizedEvent) -> Event:
    event = Event()
    _add_property(event, "uid", normalized.uid)
    _add_property(event, "dtstamp", normalized.stamp.astimezone(UTC))
    _add_property(event, "dtstart", normalized.start)
    _add_property(event, "dtend", normalized.end)
    if normalized.recurrence_id is not None:
        _add_property(event, "recurrence-id", normalized.recurrence_id)
    if normalized.out_of_office:
        _add_property(event, "summary", "Out of office")
        _add_property(event, "description", "Original status: out of office.")
        _add_property(event, "X-ICS-MERGER-ORIGINAL-STATUS", "OUT-OF-OFFICE")
    else:
        if normalized.summary is not None:
            _add_property(event, "summary", normalized.summary)
        if normalized.description is not None:
            _add_property(event, "description", normalized.description)
        if normalized.location is not None:
            _add_property(event, "location", normalized.location)
    _add_property(event, "status", normalized.status)
    _add_property(
        event,
        "transp",
        "TRANSPARENT" if normalized.out_of_office else normalized.transparency,
    )
    if normalized.availability is not None:
        _add_property(event, "X-ICS-MERGER-AVAILABILITY", normalized.availability.upper())
    return event


def _temporal_key(value: date | datetime) -> str:
    return value.isoformat()
