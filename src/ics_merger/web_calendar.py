from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Protocol, TypedDict, cast

from icalendar import Calendar
from icalendar.cal import Component


class _ComponentWalker(Protocol):
    def walk(self, name: str | None = None) -> list[Component]: ...


class WebCalendarEvent(TypedDict):
    id: str
    title: str
    start: str
    end: str
    allDay: bool
    status: str
    availability: str
    description: str | None
    location: str | None


def future_web_events(content: bytes, now: datetime) -> list[WebCalendarEvent]:
    current = now.astimezone(UTC)
    return [
        event
        for event in _project_web_events(content)
        if _ends_after(event["end"], event["allDay"], current)
    ]


@lru_cache(maxsize=3)
def _project_web_events(content: bytes) -> tuple[WebCalendarEvent, ...]:
    calendar = Calendar.from_ical(content)
    events: list[WebCalendarEvent] = []
    for component in cast(_ComponentWalker, calendar).walk("VEVENT"):
        event = _web_event(component)
        if event is not None:
            events.append(event)
    return tuple(sorted(events, key=lambda event: (event["start"], event["end"], event["id"])))


def _web_event(component: Component) -> WebCalendarEvent | None:
    start = _temporal(component, "DTSTART")
    if start is None:
        return None
    all_day = not isinstance(start, datetime)
    end = _temporal(component, "DTEND")
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    if isinstance(start, datetime) != isinstance(end, datetime):
        return None

    uid = _text(component, "UID") or _temporal_text(start)
    recurrence_id = _text(component, "RECURRENCE-ID")
    return WebCalendarEvent(
        id=f"{uid}:{recurrence_id or _temporal_text(start)}",
        title=_text(component, "SUMMARY") or "Busy",
        start=_temporal_text(start),
        end=_temporal_text(end),
        allDay=all_day,
        status=(_text(component, "STATUS") or "CONFIRMED").lower(),
        availability=(
            _text(component, "X-ICS-MERGER-AVAILABILITY") or _text(component, "TRANSP") or "BUSY"
        ).lower(),
        description=_text(component, "DESCRIPTION"),
        location=_text(component, "LOCATION"),
    )


def _temporal(component: Component, name: str) -> date | datetime | None:
    if component.get(name) is None:
        return None
    value: object = component.decoded(name)
    return value if isinstance(value, date) else None


def _text(component: Component, name: str) -> str | None:
    value = component.get(name)
    return str(value) if value is not None else None


def _temporal_text(value: date | datetime) -> str:
    return value.isoformat()


def _ends_after(value: str, all_day: bool, now: datetime) -> bool:
    if all_day:
        return date.fromisoformat(value) > now.date()
    end = datetime.fromisoformat(value)
    comparable_now = now if end.tzinfo is not None else now.replace(tzinfo=None)
    return end > comparable_now
