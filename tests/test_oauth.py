from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2 as httpx
import pytest
from authlib.integrations.base_client.errors import OAuthError
from fastapi import FastAPI
from pydantic import SecretStr

from ics_merger.app import create_app
from ics_merger.config import Settings
from ics_merger.oauth import (
    AuthlibOAuthClient,
    OAuthClientError,
    OAuthProviderConfig,
    TokenUpdate,
    provider_config,
)
from ics_merger.oauth_state import OAuthTransaction
from ics_merger.token_store import Token, TokenStore


@asynccontextmanager
async def app_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


def configured_settings(tmp_path: Path) -> Settings:
    return Settings(
        google_client_id="google-client",
        google_client_secret=SecretStr("google-secret"),
        microsoft_client_id="microsoft-client",
        microsoft_client_secret=SecretStr("microsoft-secret"),
        token_file_path=tmp_path / "tokens.json",
    )


class FakeOAuthClient:
    def __init__(
        self,
        config: OAuthProviderConfig,
        exchanged_token: Token,
        fail_exchange: bool,
    ) -> None:
        self.config = config
        self.exchanged_token = exchanged_token
        self.fail_exchange = fail_exchange
        self.verifier: str | None = None

    def create_authorization_url(
        self, transaction: OAuthTransaction, parameters: Mapping[str, str]
    ) -> str:
        query = {
            "state": transaction.state,
            "code_challenge": transaction.code_challenge,
            "code_challenge_method": "S256",
            **parameters,
        }
        return f"https://authorize.example/path?{urlencode(query)}"

    async def exchange_code(self, code: str, code_verifier: str) -> Token:
        del code
        self.verifier = code_verifier
        if self.fail_exchange:
            raise httpx.ConnectError("private provider detail")
        return self.exchanged_token

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        del url, params, headers
        raise AssertionError("Provider API request was not expected")

    async def close(self) -> None:
        pass


class FakeOAuthFactory:
    def __init__(self, fail_exchange: bool = False) -> None:
        self.fail_exchange = fail_exchange
        self.clients: list[FakeOAuthClient] = []

    def __call__(
        self,
        config: OAuthProviderConfig,
        token: Token | None = None,
        token_update: TokenUpdate | None = None,
    ) -> FakeOAuthClient:
        del token, token_update
        client = FakeOAuthClient(
            config,
            {"access_token": "stored-access", "refresh_token": "stored-refresh"},
            self.fail_exchange,
        )
        self.clients.append(client)
        return client


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
async def test_provider_login_is_disabled_without_credentials(provider: str) -> None:
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(Settings(), source_client)
    async with app_client(app) as client:
        response = await client.get(f"/api/auth/{provider}/login")
    await source_client.aclose()

    assert response.status_code == 503
    assert response.json() == {"detail": "Calendar provider is not configured"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "host", "scope", "offline_parameter"),
    [
        (
            "google",
            "accounts.google.com",
            "https://www.googleapis.com/auth/calendar.readonly",
            ("access_type", "offline"),
        ),
        (
            "microsoft",
            "login.microsoftonline.com",
            "offline_access https://graph.microsoft.com/Calendars.Read",
            ("response_mode", "query"),
        ),
    ],
)
async def test_login_url_has_pkce_state_and_minimal_scopes(
    tmp_path: Path,
    provider: str,
    host: str,
    scope: str,
    offline_parameter: tuple[str, str],
) -> None:
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(configured_settings(tmp_path), source_client)
    async with app_client(app) as client:
        response = await client.get(f"/api/auth/{provider}/login")
    await source_client.aclose()

    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert response.status_code == 302
    assert location.hostname == host
    assert query["response_type"] == ["code"]
    assert query["scope"] == [scope]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32
    assert len(query["code_challenge"][0]) >= 43
    assert query[offline_parameter[0]] == [offline_parameter[1]]


@pytest.mark.asyncio
async def test_callback_persists_token_and_redirects_without_code(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path)
    factory = FakeOAuthFactory()
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(settings, source_client, oauth_client_factory=factory)
    async with app_client(app) as client:
        login = await client.get("/api/auth/google/login")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        callback = await client.get(
            "/api/auth/google/callback", params={"state": state, "code": "private-code"}
        )
        replay = await client.get(
            "/api/auth/google/callback", params={"state": state, "code": "private-code"}
        )
    await source_client.aclose()

    stored = TokenStore(settings.token_file_path).get("google")
    assert stored == {"access_token": "stored-access", "refresh_token": "stored-refresh"}
    assert callback.status_code == 303
    assert callback.headers["location"] == "/api/auth/google/status"
    assert replay.status_code == 400
    assert replay.json() == {"detail": "Invalid authorization state"}
    assert factory.clients[-1].verifier is not None


@pytest.mark.asyncio
async def test_callback_exchange_failure_is_sanitized(tmp_path: Path) -> None:
    factory = FakeOAuthFactory(fail_exchange=True)
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(configured_settings(tmp_path), source_client, oauth_client_factory=factory)
    async with app_client(app) as client:
        login = await client.get("/api/auth/microsoft/login")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        response = await client.get(
            "/api/auth/microsoft/callback", params={"state": state, "code": "private-code"}
        )
    await source_client.aclose()

    assert response.status_code == 400
    assert response.json() == {"detail": "Authorization failed"}
    assert "private" not in response.text


@pytest.mark.asyncio
async def test_callback_without_code_consumes_state(tmp_path: Path) -> None:
    factory = FakeOAuthFactory()
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(configured_settings(tmp_path), source_client, oauth_client_factory=factory)
    async with app_client(app) as client:
        login = await client.get("/api/auth/google/login")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        missing = await client.get("/api/auth/google/callback", params={"state": state})
        replay = await client.get(
            "/api/auth/google/callback", params={"state": state, "code": "private-code"}
        )
    await source_client.aclose()

    assert missing.status_code == 400
    assert missing.json() == {"detail": "Authorization code is missing"}
    assert replay.json() == {"detail": "Invalid authorization state"}


@pytest.mark.asyncio
async def test_authlib_oauth_error_is_sanitized_by_provider_client() -> None:
    config = provider_config(configured_settings(Path("/tmp")), "google")
    assert config is not None
    client = AuthlibOAuthClient(config)

    class InvalidGrantClient:
        async def get(self, *args: object, **kwargs: object) -> httpx.Response:
            del args, kwargs
            raise OAuthError("invalid_grant")

        async def aclose(self) -> None:
            pass

    client._client = InvalidGrantClient()  # type: ignore[assignment]
    try:
        with pytest.raises(OAuthClientError) as error:
            await client.get("https://www.googleapis.com/calendar/v3/calendars/primary/events")
        assert error.value.reauthorization_required
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provider_status_and_disconnect_remove_local_token(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path)
    factory = FakeOAuthFactory()
    source_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail()))
    app = create_app(settings, source_client, oauth_client_factory=factory)
    async with app_client(app) as client:
        TokenStore(settings.token_file_path).set("google", {"access_token": "stored-access"})
        before = await client.get("/api/auth/providers")
        disconnected = await client.delete("/api/auth/google/token")
        repeated = await client.delete("/api/auth/google/token")
        after = await client.get("/api/auth/providers")
    await source_client.aclose()

    assert before.json()["google"] == {"configured": True, "connected": True}
    assert disconnected.json() == {"connected": False, "removed": True}
    assert repeated.json() == {"connected": False, "removed": False}
    assert after.json()["google"] == {"configured": True, "connected": False}
