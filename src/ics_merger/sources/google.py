from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import cast
from urllib.parse import quote

from ics_merger.event_model import (
    EventConversionError,
    NormalizedEvent,
    parse_date,
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


class GoogleCalendarSource(ProviderCalendarSource):
    provider = "google"
    default_name = "Google"

    async def _fetch_events(self, client: OAuthClient, now: datetime) -> list[NormalizedEvent]:
        url = (
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{quote(self._calendar_id, safe='')}/events"
        )
        params: dict[str, str] = {
            "singleEvents": "true",
            "showDeleted": "false",
            "orderBy": "startTime",
            "timeMin": utc_parameter(now),
            "timeMax": utc_parameter(now + timedelta(days=self._horizon_days)),
            "maxResults": "2500",
        }
        events: list[NormalizedEvent] = []
        while True:
            response = await client.get(url, params=params)
            raise_for_provider_status(response)
            payload = object_payload(response)
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE)
            for item in cast(list[object], items):
                if not isinstance(item, dict):
                    raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE)
                converted = _convert_event(cast(dict[str, object], item), self._calendar_id)
                if converted is not None:
                    events.append(converted)
            page_token = payload.get("nextPageToken")
            if page_token is None:
                return events
            if not isinstance(page_token, str) or not page_token:
                raise SourceError(SourceErrorCode.INVALID_PAGINATION)
            params = {**params, "pageToken": page_token}


def _convert_event(item: Mapping[str, object], calendar_id: str) -> NormalizedEvent | None:
    if item.get("status") == "cancelled":
        return None
    try:
        start = _google_time(item.get("start"))
        end = _google_time(item.get("end"))
        recurrence_id = _optional_google_time(item.get("originalStartTime"))
        recurrence_identity = recurrence_id.isoformat() if recurrence_id is not None else ""
        event_id = text(item.get("id")) or recurrence_identity
        if not event_id:
            raise EventConversionError
        uid = text(item.get("iCalUID")) or stable_fallback_uid(
            "google", calendar_id, event_id, recurrence_identity
        )
        stamp_value = item.get("updated")
        stamp = (
            parse_rfc3339(stamp_value)
            if stamp_value is not None
            else datetime(1970, 1, 1, tzinfo=UTC)
        )
        out_of_office = item.get("eventType") == "outOfOffice"
        transparency = "TRANSPARENT" if item.get("transparency") == "transparent" else "OPAQUE"
        status = "TENTATIVE" if item.get("status") == "tentative" else "CONFIRMED"
        return NormalizedEvent(
            uid=uid,
            start=start,
            end=end,
            stamp=stamp,
            summary=text(item.get("summary")),
            description=text(item.get("description")),
            location=text(item.get("location")),
            status=status,
            transparency=transparency,
            availability="outOfOffice" if out_of_office else text(item.get("transparency")),
            out_of_office=out_of_office,
            recurrence_id=recurrence_id,
        )
    except EventConversionError as exc:
        raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE) from exc


def _google_time(value: object) -> date | datetime:
    if not isinstance(value, dict):
        raise EventConversionError
    time_value = cast(dict[str, object], value)
    date_value = time_value.get("date")
    if date_value is not None:
        return parse_date(date_value)
    return parse_rfc3339(time_value.get("dateTime"))


def _optional_google_time(value: object) -> date | datetime | None:
    return None if value is None else _google_time(value)
