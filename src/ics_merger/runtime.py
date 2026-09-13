import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx2

from ics_merger.config import Settings
from ics_merger.merge import CalendarMerger, MergeResult, SourceLabel
from ics_merger.oauth import OAuthClientFactory, OAuthCoordinator, provider_config
from ics_merger.oauth_state import OAuthTransactionStore
from ics_merger.sources.base import CalendarSource
from ics_merger.sources.google import GoogleCalendarSource
from ics_merger.sources.microsoft import MicrosoftCalendarSource
from ics_merger.sources.remote_ics import (
    AddressResolver,
    RemoteIcsAdapter,
    RemoteIcsPolicy,
    RemoteIcsSource,
)
from ics_merger.token_store import TokenStore


@dataclass(slots=True)
class RuntimeBundle:
    settings: Settings
    merger: CalendarMerger
    sources: list[CalendarSource]
    token_store: TokenStore | None
    oauth: OAuthCoordinator


class RuntimeManager:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx2.AsyncClient,
        resolver: AddressResolver,
        oauth_client_factory: OAuthClientFactory,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._resolver = resolver
        self._oauth_client_factory = oauth_client_factory
        self._now = now
        self._bundle: RuntimeBundle | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> Settings:
        return self._settings

    async def start(self) -> None:
        self._bundle = await self._build(self._settings)

    async def stop(self) -> None:
        async with self._lock:
            if self._bundle is not None:
                await self._bundle.merger.stop()
                self._bundle = None

    async def reload(self, settings: Settings) -> None:
        candidate = await self._build(settings)
        async with self._lock:
            previous = self._required_bundle()
            self._bundle = candidate
            self._settings = settings
            await previous.merger.stop()

    async def merge(
        self, source_label: SourceLabel, *, include_history: bool = False
    ) -> MergeResult:
        async with self._lock:
            return await self._required_bundle().merger.merge(
                source_label=source_label,
                include_history=include_history,
            )

    async def authorization_url(self, provider: str) -> str:
        async with self._lock:
            return await self._required_bundle().oauth.authorization_url(provider)

    async def exchange(self, provider: str, state: str, code: str) -> None:
        async with self._lock:
            await self._required_bundle().oauth.exchange(provider, state, code)

    async def reject(self, provider: str, state: str) -> None:
        async with self._lock:
            self._required_bundle().oauth.reject(provider, state)

    async def provider_statuses(self) -> dict[str, dict[str, bool]]:
        async with self._lock:
            bundle = self._required_bundle()
            statuses: dict[str, dict[str, bool]] = {}
            for provider in ("google", "microsoft"):
                configured = bundle.oauth.is_enabled(provider)
                connected = (
                    configured
                    and bundle.token_store is not None
                    and bundle.token_store.get(provider) is not None
                )
                statuses[provider] = {"configured": configured, "connected": connected}
            return statuses

    async def is_provider_enabled(self, provider: str) -> bool:
        async with self._lock:
            return self._required_bundle().oauth.is_enabled(provider)

    async def disconnect(self, provider: str) -> bool:
        async with self._lock:
            current = self._required_bundle()
            if not current.oauth.is_enabled(provider) or current.token_store is None:
                raise KeyError(provider)
            await current.merger.stop()
            try:
                removed = current.token_store.delete(provider)
                self._bundle = await self._build(current.settings)
                return removed
            except Exception:
                await current.merger.start(current.sources)
                raise

    def _required_bundle(self) -> RuntimeBundle:
        if self._bundle is None:
            raise RuntimeError("Calendar runtime has not been started")
        return self._bundle

    async def _build(self, settings: Settings) -> RuntimeBundle:
        token_store = self._create_token_store(settings)
        sources = self._create_sources(settings, token_store)
        merger = CalendarMerger(
            self._now,
            include_free_time=settings.include_free_time,
        )
        await merger.start(sources)
        return RuntimeBundle(
            settings=settings,
            merger=merger,
            sources=sources,
            token_store=token_store,
            oauth=OAuthCoordinator(
                settings,
                token_store,
                OAuthTransactionStore(),
                self._oauth_client_factory,
            ),
        )

    def _create_sources(
        self, settings: Settings, token_store: TokenStore | None
    ) -> list[CalendarSource]:
        policy = RemoteIcsPolicy(
            max_response_bytes=settings.max_response_bytes,
            allow_private_networks=settings.allow_private_networks,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            read_timeout_seconds=settings.read_timeout_seconds,
            write_timeout_seconds=settings.write_timeout_seconds,
            pool_timeout_seconds=settings.pool_timeout_seconds,
        )
        remote_adapter = RemoteIcsAdapter(self._http_client, policy, self._resolver)
        sources: list[CalendarSource] = [
            RemoteIcsSource(
                remote_adapter,
                calendar.url,
                calendar.name,
                calendar.ttl_seconds,
            )
            for calendar in settings.remote_ics_calendars
        ]
        google_config = provider_config(settings, "google")
        if token_store is not None and google_config is not None:
            sources.extend(
                GoogleCalendarSource(
                    calendar.calendar_id,
                    settings.future_horizon_days,
                    token_store,
                    google_config,
                    self._oauth_client_factory,
                    name=calendar.name,
                    ttl_seconds=calendar.ttl_seconds,
                )
                for calendar in settings.google_calendars
            )
        microsoft_config = provider_config(settings, "microsoft")
        if token_store is not None and microsoft_config is not None:
            sources.extend(
                MicrosoftCalendarSource(
                    calendar.calendar_id,
                    settings.future_horizon_days,
                    token_store,
                    microsoft_config,
                    self._oauth_client_factory,
                    name=calendar.name,
                    ttl_seconds=calendar.ttl_seconds,
                )
                for calendar in settings.microsoft_calendars
            )
        return sources

    @staticmethod
    def _create_token_store(settings: Settings) -> TokenStore | None:
        return (
            TokenStore(settings.token_file_path)
            if settings.google_enabled or settings.microsoft_enabled
            else None
        )
