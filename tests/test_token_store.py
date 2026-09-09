import stat
from pathlib import Path

import pytest

from ics_merger.oauth_state import InvalidOAuthStateError, OAuthTransactionStore
from ics_merger.token_store import TokenStore, TokenStoreError


def test_token_round_trip_uses_json_and_owner_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "private" / "tokens.json"
    store = TokenStore(path)
    store.set("google", {"access_token": "plain-secret", "expires_at": 1234})

    assert store.get("google") == {"access_token": "plain-secret", "expires_at": 1234}
    assert path.read_text(encoding="utf-8") == (
        '{"google":{"access_token":"plain-secret","expires_at":1234}}'
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_delete_removes_only_selected_provider(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    store.set("google", {"access_token": "google-token"})
    store.set("microsoft", {"access_token": "microsoft-token"})

    assert store.delete("google")
    assert not store.delete("google")
    assert store.get("google") is None
    assert store.get("microsoft") == {"access_token": "microsoft-token"}


def test_oauth_state_is_provider_bound_and_one_time() -> None:
    transactions = OAuthTransactionStore()
    transaction = transactions.create("google")

    with pytest.raises(InvalidOAuthStateError):
        transactions.consume("microsoft", transaction.state)
    with pytest.raises(InvalidOAuthStateError):
        transactions.consume("google", transaction.state)


def test_oauth_state_expires() -> None:
    now = 100.0
    transactions = OAuthTransactionStore(lifetime_seconds=10, clock=lambda: now)
    transaction = transactions.create("google")
    now = 111.0

    with pytest.raises(InvalidOAuthStateError):
        transactions.consume("google", transaction.state)


def test_rejects_malformed_token_structure(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text('{"google":["not-a-token"]}', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(TokenStoreError, match="Token store is invalid"):
        TokenStore(path).get("google")


def test_rejects_permissive_token_store(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(TokenStoreError, match="permissions must be 0600"):
        TokenStore(path).get("google")


def test_rejects_oversized_token_without_replacing_store(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.set("google", {"access_token": "current"})

    with pytest.raises(TokenStoreError, match="exceeds the 1 MiB size limit"):
        store.set("google", {"access_token": "x" * (1024 * 1024)})

    assert store.get("google") == {"access_token": "current"}
