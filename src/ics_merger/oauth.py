from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import httpx2

from ics_merger.config import Settings
from ics_merger.oauth_state import OAuthTransaction, OAuthTransactionStore
from ics_merger.token_store import Token, TokenStore, TokenStoreError

TokenUpdate = Callable[[Token], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class OAuthProviderConfig:
    name: str
    client_id: str
    client_secret: str
    authorization_url: str
    token_url: str
    redirect_uri: str
    scopes: tuple[str, ...]
    authorization_parameters: Mapping[str, str]


class OAuthClient(Protocol):
    def create_authorization_url(
        self, transaction: OAuthTransaction, parameters: Mapping[str, str]
    ) -> str: ...

    async def exchange_code(self, code: str, code_verifier: str) -> Token: ...

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response: ...

    async def close(self) -> None: ...


class OAuthClientFactory(Protocol):
    def __call__(
        self,
        config: OAuthProviderConfig,
        token: Token | None = None,
        token_update: TokenUpdate | None = None,
    ) -> OAuthClient: ...


class _AuthlibAuthorizationClient(Protocol):
    def create_authorization_url(
        self,
        url: str,
        *,
        state: str,
        code_verifier: str,
        **parameters: str,
    ) -> tuple[str, object]: ...

    async def fetch_token(
        self,
        url: str,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Mapping[str, object]: ...


class OAuthExchangeError(Exception):
    """A sanitized authorization-code exchange failure."""


class OAuthClientError(Exception):
    """An OAuth client failure without provider-specific details."""

    def __init__(self, *, reauthorization_required: bool = False) -> None:
        self.reauthorization_required = reauthorization_required


class AuthlibOAuthClient:
    def __init__(
        self,
        config: OAuthProviderConfig,
        token: Token | None = None,
        token_update: TokenUpdate | None = None,
    ) -> None:
        from authlib.common.errors import AuthlibBaseError
        from authlib.integrations.httpx_client import AsyncOAuth2Client

        self._config = config
        self._token_update = token_update
        self._oauth_error: type[Exception] = AuthlibBaseError
        self._client = AsyncOAuth2Client(
            client_id=config.client_id,
            client_secret=config.client_secret,
            token_endpoint_auth_method="client_secret_post",
            scope=" ".join(config.scopes),
            redirect_uri=config.redirect_uri,
            token=token,
            update_token=self._handle_token_update if token_update is not None else None,
            code_challenge_method="S256",
            token_endpoint=config.token_url,
            timeout=httpx2.Timeout(10.0),
            trust_env=False,
        )

    def create_authorization_url(
        self, transaction: OAuthTransaction, parameters: Mapping[str, str]
    ) -> str:
        try:
            client = cast(_AuthlibAuthorizationClient, self._client)
            url, _ = client.create_authorization_url(
                self._config.authorization_url,
                state=transaction.state,
                code_verifier=transaction.code_verifier,
                **parameters,
            )
            return url
        except self._oauth_error as exc:
            raise OAuthClientError from exc

    async def exchange_code(self, code: str, code_verifier: str) -> Token:
        try:
            client = cast(_AuthlibAuthorizationClient, self._client)
            token = await client.fetch_token(
                self._config.token_url,
                code=code,
                code_verifier=code_verifier,
                redirect_uri=self._config.redirect_uri,
            )
            return _normalize_token(token)
        except self._oauth_error as exc:
            raise OAuthClientError from exc

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        try:
            http_client = cast(httpx2.AsyncClient, self._client)
            return await http_client.get(url, params=params, headers=headers)
        except self._oauth_error as exc:
            raise OAuthClientError(
                reauthorization_required=getattr(exc, "error", None) == "invalid_grant"
            ) from exc

    async def close(self) -> None:
        await cast(httpx2.AsyncClient, self._client).aclose()

    async def _handle_token_update(self, token: Mapping[str, object], **_: object) -> None:
        if self._token_update is not None:
            await self._token_update(_normalize_token(token))


def authlib_client_factory(
    config: OAuthProviderConfig,
    token: Token | None = None,
    token_update: TokenUpdate | None = None,
) -> OAuthClient:
    return AuthlibOAuthClient(config, token, token_update)


class OAuthCoordinator:
    def __init__(
        self,
        settings: Settings,
        token_store: TokenStore | None,
        transactions: OAuthTransactionStore,
        client_factory: OAuthClientFactory = authlib_client_factory,
    ) -> None:
        self._configs = _provider_configs(settings)
        self._token_store = token_store
        self._transactions = transactions
        self._client_factory = client_factory

    def is_enabled(self, provider: str) -> bool:
        return provider in self._configs and self._token_store is not None

    async def authorization_url(self, provider: str) -> str:
        config = self._enabled_config(provider)
        transaction = self._transactions.create(provider)
        client = self._client_factory(config)
        try:
            return client.create_authorization_url(transaction, config.authorization_parameters)
        finally:
            await client.close()

    async def exchange(self, provider: str, state: str, code: str) -> None:
        config = self._enabled_config(provider)
        code_verifier = self._transactions.consume(provider, state)
        client = self._client_factory(config)
        try:
            token = await client.exchange_code(code, code_verifier)
            assert self._token_store is not None
            existing = self._token_store.get(provider)
            if existing is not None and "refresh_token" not in token:
                refresh_token = existing.get("refresh_token")
                if isinstance(refresh_token, str):
                    token["refresh_token"] = refresh_token
            self._token_store.set(provider, token)
        except (OAuthClientError, httpx2.HTTPError, TokenStoreError, ValueError) as exc:
            raise OAuthExchangeError from exc
        finally:
            await client.close()

    def reject(self, provider: str, state: str) -> None:
        self._enabled_config(provider)
        self._transactions.consume(provider, state)

    def _enabled_config(self, provider: str) -> OAuthProviderConfig:
        config = self._configs.get(provider)
        if config is None or self._token_store is None:
            raise KeyError(provider)
        return config


def _provider_configs(settings: Settings) -> dict[str, OAuthProviderConfig]:
    configs: dict[str, OAuthProviderConfig] = {}
    if settings.google_enabled:
        assert settings.google_client_id is not None
        assert settings.google_client_secret is not None
        configs["google"] = OAuthProviderConfig(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value(),
            authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            redirect_uri=settings.callback_url("google"),
            scopes=("https://www.googleapis.com/auth/calendar.readonly",),
            authorization_parameters={"access_type": "offline", "prompt": "consent"},
        )
    if settings.microsoft_enabled:
        assert settings.microsoft_client_id is not None
        assert settings.microsoft_client_secret is not None
        tenant = settings.microsoft_tenant
        configs["microsoft"] = OAuthProviderConfig(
            name="microsoft",
            client_id=settings.microsoft_client_id,
            client_secret=settings.microsoft_client_secret.get_secret_value(),
            authorization_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            redirect_uri=settings.callback_url("microsoft"),
            scopes=("offline_access", "https://graph.microsoft.com/Calendars.Read"),
            authorization_parameters={"response_mode": "query"},
        )
    return configs


def provider_config(settings: Settings, provider: str) -> OAuthProviderConfig | None:
    return _provider_configs(settings).get(provider)


def _normalize_token(token: Mapping[str, object]) -> Token:
    normalized: Token = {}
    for key, value in token.items():
        if isinstance(value, str | int | float | bool) or value is None:
            normalized[key] = value
    if not isinstance(normalized.get("access_token"), str):
        raise ValueError("OAuth token has no access token")
    return normalized
