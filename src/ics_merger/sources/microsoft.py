from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import cast
from urllib.parse import quote

import httpx2

from ics_merger.event_model import (
    EventConversionError,
    NormalizedEvent,
    parse_rfc3339,
    stable_fallback_uid,
    text,
)
from ics_merger.oauth import OAuthClient
from ics_merger.sources.errors import SourceError, SourceErrorCode
from ics_merger.sources.provider import (
    ProviderCalendarSource,
    object_payload,
    raise_for_provider_status,
    utc_parameter,
)

_GRAPH_ORIGIN = "https://graph.microsoft.com"


class MicrosoftCalendarSource(ProviderCalendarSource):
    provider = "microsoft"
    default_name = "Microsoft"

    async def _fetch_events(self, client: OAuthClient, now: datetime) -> list[NormalizedEvent]:
        url = self._initial_url()
        params: dict[str, str] | None = {
            "startDateTime": utc_parameter(now - timedelta(days=self._horizon_days)),
            "endDateTime": utc_parameter(now + timedelta(days=self._horizon_days)),
            "$top": "1000",
            "$select": (
                "id,iCalUId,subject,bodyPreview,location,start,end,isAllDay,isCancelled,"
                "showAs,type,seriesMasterId,originalStart,lastModifiedDateTime"
            ),
        }
        headers = {"Prefer": 'outlook.timezone="UTC"'}
        events: list[NormalizedEvent] = []
        while True:
            response = await client.get(url, params=params, headers=headers)
            raise_for_provider_status(response)
            payload = object_payload(response)
            items = payload.get("value", [])
            if not isinstance(items, list):
                raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE)
            for item in cast(list[object], items):
                if not isinstance(item, dict):
                    raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE)
                converted = _convert_event(cast(dict[str, object], item), self._calendar_id)
                if converted is not None:
                    events.append(converted)
            next_link = payload.get("@odata.nextLink")
            if next_link is None:
                return events
            if not isinstance(next_link, str) or not _valid_next_link(next_link):
                raise SourceError(SourceErrorCode.INVALID_PAGINATION)
            url = next_link
            params = None

    def _initial_url(self) -> str:
        if self._calendar_id == "primary":
            return f"{_GRAPH_ORIGIN}/v1.0/me/calendarView"
        calendar_id = quote(self._calendar_id, safe="")
        return f"{_GRAPH_ORIGIN}/v1.0/me/calendars/{calendar_id}/calendarView"


def _convert_event(item: Mapping[str, object], calendar_id: str) -> NormalizedEvent | None:
    if item.get("isCancelled") is True:
        return None
    try:
        all_day = item.get("isAllDay") is True
        start = _graph_time(item.get("start"), all_day=all_day)
        end = _graph_time(item.get("end"), all_day=all_day)
        recurrence_id = _optional_graph_time(item.get("originalStart"), all_day=all_day)
        recurrence_identity = recurrence_id.isoformat() if recurrence_id is not None else ""
        event_id = text(item.get("id")) or recurrence_identity
        if not event_id:
            raise EventConversionError
        uid = text(item.get("iCalUId")) or stable_fallback_uid(
            "microsoft", calendar_id, event_id, recurrence_identity
        )
        stamp_value = item.get("lastModifiedDateTime")
        stamp = (
            parse_rfc3339(stamp_value, assume_utc=True)
            if stamp_value is not None
            else datetime(1970, 1, 1, tzinfo=UTC)
        )
        show_as = text(item.get("showAs")) or "busy"
        out_of_office = show_as == "oof"
        transparency = "TRANSPARENT" if show_as in {"free", "workingElsewhere", "oof"} else "OPAQUE"
        location_value = item.get("location")
        location = (
            text(cast(dict[str, object], location_value).get("displayName"))
            if isinstance(location_value, dict)
            else None
        )
        return NormalizedEvent(
            uid=uid,
            start=start,
            end=end,
            stamp=stamp,
            summary=text(item.get("subject")),
            description=text(item.get("bodyPreview")),
            location=location,
            status="TENTATIVE" if show_as == "tentative" else "CONFIRMED",
            transparency=transparency,
            availability=show_as,
            out_of_office=out_of_office,
            recurrence_id=recurrence_id,
        )
    except EventConversionError as exc:
        raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE) from exc


def _graph_time(value: object, *, all_day: bool) -> date | datetime:
    if not isinstance(value, dict):
        raise EventConversionError
    parsed = parse_rfc3339(
        cast(dict[str, object], value).get("dateTime"), assume_utc=True
    ).astimezone(UTC)
    return parsed.date() if all_day else parsed


def _optional_graph_time(value: object, *, all_day: bool) -> date | datetime | None:
    if value is None:
        return None
    parsed = parse_rfc3339(value, assume_utc=True).astimezone(UTC)
    return parsed.date() if all_day else parsed


def _valid_next_link(value: str) -> bool:
    try:
        url = httpx2.URL(value)
    except httpx2.InvalidURL:
        return False
    return (
        url.scheme == "https"
        and url.host == "graph.microsoft.com"
        and url.port in {None, 443}
        and url.userinfo == b""
        and url.path.startswith("/v1.0/")
    )
