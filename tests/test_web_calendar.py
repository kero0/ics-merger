from datetime import UTC, date, datetime
from typing import Protocol, cast

from icalendar import Calendar, Event
from icalendar.cal import Component

from ics_merger.web_calendar import future_web_events


class _PropertyWriter(Protocol):
    def add(self, name: str, value: object) -> None: ...


def _add_property(component: Component, name: str, value: object) -> None:
    cast(_PropertyWriter, component).add(name, value)


def test_future_web_events_start_at_now_and_preserve_all_day_events() -> None:
    calendar = Calendar()
    _add_property(calendar, "version", "2.0")
    calendar.add_component(
        _event("past", datetime(2026, 9, 5, 8, tzinfo=UTC), datetime(2026, 9, 5, 9, tzinfo=UTC))
    )
    calendar.add_component(
        _event(
            "ongoing",
            datetime(2026, 9, 5, 11, tzinfo=UTC),
            datetime(2026, 9, 5, 13, tzinfo=UTC),
        )
    )
    calendar.add_component(_event("all-day", date(2026, 9, 6), date(2026, 9, 7)))

    events = future_web_events(calendar.to_ical(), datetime(2026, 9, 5, 12, tzinfo=UTC))

    assert [event["title"] for event in events] == ["ongoing", "all-day"]
    assert events[0]["start"] == "2026-09-05T11:00:00+00:00"
    assert events[1]["allDay"] is True

    later_events = future_web_events(calendar.to_ical(), datetime(2026, 9, 5, 13, tzinfo=UTC))
    assert [event["title"] for event in later_events] == ["all-day"]


def _event(uid: str, start: date | datetime, end: date | datetime) -> Event:
    event = Event()
    _add_property(event, "uid", uid)
    _add_property(event, "summary", uid)
    _add_property(event, "dtstart", start)
    _add_property(event, "dtend", end)
    return event
