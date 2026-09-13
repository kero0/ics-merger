from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx2
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from ics_merger.config import ConfigDocument, ConfigurationError, Settings
from ics_merger.config_repository import ConfigConflictError, ConfigRepository, ConfigWriteError
from ics_merger.merge import AllSourcesFailedError, MergeResult, SourceLabel
from ics_merger.oauth import (
    OAuthClientFactory,
    OAuthExchangeError,
    authlib_client_factory,
)
from ics_merger.oauth_state import InvalidOAuthStateError
from ics_merger.runtime import RuntimeManager
from ics_merger.sources.remote_ics import AddressResolver
from ics_merger.sources.remote_ics import resolve_addresses as default_resolver
from ics_merger.token_store import TokenStoreError
from ics_merger.web_calendar import web_events

_WEB_DIRECTORY = Path(__file__).with_name("web")


def create_app(
    settings: Settings | None = None,
    http_client: httpx2.AsyncClient | None = None,
    resolver: AddressResolver = default_resolver,
    oauth_client_factory: OAuthClientFactory | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    config_path: Path | None = None,
) -> FastAPI:
    service_settings = settings or Settings()
    owns_client = http_client is None
    config_repository = ConfigRepository(config_path) if config_path is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        client = http_client or httpx2.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            limits=httpx2.Limits(max_connections=20, max_keepalive_connections=10),
        )
        manager = RuntimeManager(
            service_settings,
            client,
            resolver,
            oauth_client_factory or authlib_client_factory,
            now,
        )
        app.state.runtime = manager
        app.state.now = now
        try:
            await manager.start()
            yield
        finally:
            await manager.stop()
            if owns_client:
                await client.aclose()

    application = FastAPI(
        title="ICS Merger",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    @application.get("/", response_class=FileResponse, include_in_schema=False)
    async def web_calendar() -> FileResponse:
        return FileResponse(_WEB_DIRECTORY / "index.html", media_type="text/html")

    @application.get("/settings", response_class=FileResponse, include_in_schema=False)
    async def web_settings() -> FileResponse:
        return FileResponse(_WEB_DIRECTORY / "settings.html", media_type="text/html")

    @application.get("/assets/app.css", response_class=FileResponse, include_in_schema=False)
    async def web_styles() -> FileResponse:
        return FileResponse(_WEB_DIRECTORY / "app.css", media_type="text/css")

    @application.get("/assets/app.js", response_class=FileResponse, include_in_schema=False)
    async def web_script() -> FileResponse:
        return FileResponse(_WEB_DIRECTORY / "app.js", media_type="text/javascript")

    @application.get("/assets/settings.css", response_class=FileResponse, include_in_schema=False)
    async def settings_styles() -> FileResponse:
        return FileResponse(_WEB_DIRECTORY / "settings.css", media_type="text/css")

    @application.get("/assets/settings.js", response_class=FileResponse, include_in_schema=False)
    async def settings_script() -> FileResponse:
        return FileResponse(_WEB_DIRECTORY / "settings.js", media_type="text/javascript")

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/auth/{provider}/login", response_class=RedirectResponse)
    async def oauth_login(provider: str, request: Request) -> RedirectResponse:
        manager: RuntimeManager = request.app.state.runtime
        if provider not in {"google", "microsoft"} or not await manager.is_provider_enabled(
            provider
        ):
            raise HTTPException(status_code=503, detail="Calendar provider is not configured")
        return RedirectResponse(await manager.authorization_url(provider), status_code=302)

    @application.get("/api/auth/{provider}/callback", response_class=RedirectResponse)
    async def oauth_callback(
        provider: str,
        request: Request,
        state: str | None = Query(default=None),
        code: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> RedirectResponse:
        manager: RuntimeManager = request.app.state.runtime
        if provider not in {"google", "microsoft"} or not await manager.is_provider_enabled(
            provider
        ):
            raise HTTPException(status_code=503, detail="Calendar provider is not configured")
        if state is None:
            raise HTTPException(status_code=400, detail="Invalid authorization state")
        try:
            if error is not None:
                await manager.reject(provider, state)
                raise HTTPException(status_code=400, detail="Authorization was not completed")
            if code is None:
                await manager.reject(provider, state)
                raise HTTPException(status_code=400, detail="Authorization code is missing")
            await manager.exchange(provider, state, code)
        except InvalidOAuthStateError as exc:
            raise HTTPException(status_code=400, detail="Invalid authorization state") from exc
        except OAuthExchangeError as exc:
            raise HTTPException(status_code=400, detail="Authorization failed") from exc
        return RedirectResponse(f"/api/auth/{provider}/status", status_code=303)

    @application.get("/api/auth/{provider}/status", response_class=HTMLResponse)
    async def oauth_status(provider: str) -> HTMLResponse:
        if provider not in {"google", "microsoft"}:
            raise HTTPException(status_code=404, detail="Unknown calendar provider")
        return HTMLResponse(
            "<!doctype html><title>Calendar authorization</title>"
            "<p>Calendar authorization completed. You may close this page.</p>"
            "<script>window.opener?.postMessage('ics-merger-auth-complete', location.origin);"
            "window.close();</script>"
        )

    def repository() -> ConfigRepository:
        if config_repository is None:
            raise HTTPException(status_code=503, detail="Configuration editing is unavailable")
        return config_repository

    @application.get("/api/config")
    async def get_config(request: Request) -> dict[str, Any]:
        try:
            stored = repository().read()
            manager: RuntimeManager = request.app.state.runtime
            return {
                "revision": stored.revision,
                "config": stored.document.model_dump(mode="json", exclude_none=True),
                "providers": await manager.provider_statuses(),
            }
        except (ConfigurationError, TokenStoreError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @application.put("/api/config")
    async def update_config(
        payload: dict[str, Any],
        request: Request,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if if_match is None:
            raise HTTPException(status_code=428, detail="If-Match revision is required")
        try:
            document = ConfigDocument.model_validate(payload)
        except ValidationError as exc:
            details = [
                {
                    "location": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
                for item in exc.errors(include_url=False, include_input=False)
            ]
            raise HTTPException(status_code=422, detail=details) from exc
        manager: RuntimeManager = request.app.state.runtime
        previous = manager.settings
        config_store = repository()
        current = config_store.read()
        try:
            stored = config_store.save(document, if_match.strip('"'))
        except ConfigConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="Configuration changed; reload and retry",
            ) from exc
        except (ConfigurationError, ConfigWriteError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            await manager.reload(stored.settings)
        except Exception as exc:
            try:
                config_store.save(current.document, stored.revision)
                detail = "Configuration was not applied; the previous configuration was restored"
            except (ConfigConflictError, ConfigurationError, ConfigWriteError):
                detail = "Configuration was saved but could not be applied or restored"
            raise HTTPException(
                status_code=500,
                detail=detail,
            ) from exc
        return {
            "revision": stored.revision,
            "config": stored.document.model_dump(mode="json", exclude_none=True),
            "restartRequired": (
                previous.host != stored.settings.host or previous.port != stored.settings.port
            ),
            "providers": await manager.provider_statuses(),
        }

    @application.get("/api/auth/providers")
    async def provider_statuses(request: Request) -> dict[str, dict[str, bool]]:
        manager: RuntimeManager = request.app.state.runtime
        try:
            return await manager.provider_statuses()
        except TokenStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @application.delete("/api/auth/{provider}/token")
    async def disconnect_provider(provider: str, request: Request) -> dict[str, bool]:
        if provider not in {"google", "microsoft"}:
            raise HTTPException(status_code=404, detail="Unknown calendar provider")
        manager: RuntimeManager = request.app.state.runtime
        try:
            removed = await manager.disconnect(provider)
        except KeyError as exc:
            raise HTTPException(
                status_code=503,
                detail="Calendar provider is not configured",
            ) from exc
        except TokenStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"connected": False, "removed": removed}

    async def merge_sources(
        request: Request,
        source_label: SourceLabel,
        *,
        include_history: bool = False,
    ) -> MergeResult:
        manager: RuntimeManager = request.app.state.runtime
        try:
            return await manager.merge(source_label, include_history=include_history)
        except AllSourcesFailedError as exc:
            raise HTTPException(
                status_code=503,
                detail="No calendar source is currently available",
            ) from exc

    @application.get("/api/calendars/merged.ics", response_class=Response)
    async def merged_calendar(
        request: Request,
        source_label: Annotated[SourceLabel, Query()] = SourceLabel.NONE,
    ) -> Response:
        result = await merge_sources(request, source_label)
        return Response(
            content=result.content,
            media_type="text/calendar",
            headers={
                "X-ICS-Merger-Sources": str(result.source_count),
                "X-ICS-Merger-Source-Failures": str(result.failed_source_count),
            },
        )

    @application.get("/api/calendars/merged.json", response_class=JSONResponse)
    async def merged_calendar_json(
        request: Request,
        source_label: Annotated[SourceLabel, Query()] = SourceLabel.NONE,
    ) -> JSONResponse:
        result = await merge_sources(request, source_label, include_history=True)
        clock: Callable[[], datetime] = request.app.state.now
        current = clock().astimezone(UTC)
        return JSONResponse(
            {
                "generatedAt": current.isoformat(),
                "sourceCount": result.source_count,
                "failedSourceCount": result.failed_source_count,
                "events": web_events(result.content),
            }
        )

    return application


app = create_app()
