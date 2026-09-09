from typing import Protocol

from icalendar import Calendar


class CalendarSource(Protocol):
    """One independently failing source of calendar events."""

    @property
    def name(self) -> str: ...

    @property
    def ttl_seconds(self) -> float: ...

    async def fetch(self) -> Calendar: ...
