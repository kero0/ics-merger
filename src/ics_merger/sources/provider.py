from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import ClassVar, cast

import httpx2
from icalendar import Calendar

from ics_merger.event_model import NormalizedEvent, provider_calendar
from ics_merger.oauth import (
    OAuthClient,
    OAuthClientError,
    OAuthClientFactory,
    OAuthProviderConfig,
)
from ics_merger.sources.errors import SourceError, SourceErrorCode
from ics_merger.token_store import Token, TokenStore, TokenStoreError


class ProviderCalendarSource(ABC):
    provider: ClassVar[str]
    default_name: ClassVar[str]

    def __init__(
        self,
        calendar_id: str,
        horizon_days: int,
        token_store: TokenStore,
        oauth_config: OAuthProviderConfig,
        client_factory: OAuthClientFactory,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        name: str | None = None,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self.name = name or self.default_name
        self.ttl_seconds = ttl_seconds
        self._calendar_id = calendar_id
        self._horizon_days = horizon_days
        self._token_store = token_store
        self._oauth_config = oauth_config
        self._client_factory = client_factory
        self._now = now

    async def fetch(self) -> Calendar:
        token = self._load_token()

        async def update_token(updated: Token) -> None:
            refresh_token = token.get("refresh_token")
            if "refresh_token" not in updated and isinstance(refresh_token, str):
                updated["refresh_token"] = refresh_token
            self._store_token(updated)

        client = self._client_factory(self._oauth_config, token, update_token)
        try:
            events = await self._fetch_events(client, self._now().astimezone(UTC))
            return provider_calendar(events)
        except SourceError:
            raise
        except OAuthClientError as exc:
            if exc.reauthorization_required:
                try:
                    self._token_store.delete(self.provider)
                except TokenStoreError:
                    pass
            raise SourceError(SourceErrorCode.AUTH_REQUIRED) from exc
        except httpx2.HTTPError as exc:
            raise SourceError(SourceErrorCode.PROVIDER_FETCH_FAILED) from exc
        finally:
            await client.close()

    @abstractmethod
    async def _fetch_events(self, client: OAuthClient, now: datetime) -> list[NormalizedEvent]: ...

    def _load_token(self) -> Token:
        try:
            token = self._token_store.get(self.provider)
        except TokenStoreError as exc:
            raise SourceError(SourceErrorCode.AUTH_REQUIRED) from exc
        if token is None:
            raise SourceError(SourceErrorCode.AUTH_REQUIRED)
        return token

    def _store_token(self, token: Token) -> None:
        try:
            self._token_store.set(self.provider, token)
        except TokenStoreError as exc:
            raise SourceError(SourceErrorCode.AUTH_REQUIRED) from exc


def object_payload(response: httpx2.Response) -> dict[str, object]:
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE) from exc
    if not isinstance(payload, dict):
        raise SourceError(SourceErrorCode.MALFORMED_PROVIDER_RESPONSE)
    return cast(dict[str, object], payload)


def raise_for_provider_status(response: httpx2.Response) -> None:
    if response.status_code in {401, 403}:
        raise SourceError(SourceErrorCode.AUTH_REQUIRED)
    if not response.is_success:
        raise SourceError(SourceErrorCode.PROVIDER_FETCH_FAILED)


def utc_parameter(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
