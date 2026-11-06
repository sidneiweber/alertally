from types import SimpleNamespace

import pytest

import app as app_module
import routes.auth as auth
from config import Config
from conftest import USER, login


@pytest.mark.parametrize("email, allowed", [
    ("ana@example.com", True),
    ("ANA@Example.COM", True),            # sem diferenciar maiúsculas
    ("guest@example.net", True),          # ALLOWED_EMAILS
    ("admin@example.org", True),          # admins sempre entram
    ("other@example.net", False),
    ("ana@sub.example.com", False),       # subdomínio não conta
    ("ana@example.com.evil.io", False),
    ("ana@notexample.com", False),
    ("not-an-email", False),
    ("", False),
    (None, False),
])
def test_is_email_allowed(email, allowed):
    assert auth.is_email_allowed(email) is allowed


def test_anonymous_is_sent_to_google_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login/google")


def test_allowed_user_sees_home(client, alerts):
    login(client)
    assert client.get("/").status_code == 200


def test_session_of_user_no_longer_allowed_is_dropped(client):
    login(client, {"name": "X", "email": "x@other.io"})
    resp = client.get("/")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "user" not in sess


def _fake_google(monkeypatch, user_info, ok=True):
    fake = SimpleNamespace(
        authorized=True,
        get=lambda url: SimpleNamespace(ok=ok, json=lambda: user_info),
    )
    monkeypatch.setattr(auth, "google", fake)


def test_google_callback_allows_domain_user(client, monkeypatch):
    _fake_google(monkeypatch, {"email": "Ana@example.com", "verified_email": True, "name": "Ana"})
    resp = client.get("/google_authorized")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess["user"]["email"] == "ana@example.com"


def test_google_callback_denies_other_domain(client, monkeypatch):
    _fake_google(monkeypatch, {"email": "someone@gmail.com", "verified_email": True, "name": "S"})
    with client.session_transaction() as sess:
        sess["google_oauth_token"] = {"access_token": "t"}
    resp = client.get("/google_authorized")
    assert resp.status_code == 403
    assert b"someone@gmail.com" in resp.data
    with client.session_transaction() as sess:
        assert "user" not in sess
        assert "google_oauth_token" not in sess


def test_google_callback_denies_unverified_email(client, monkeypatch):
    _fake_google(monkeypatch, {"email": "ana@example.com", "verified_email": False})
    assert client.get("/google_authorized").status_code == 403


def test_access_denied_page_is_translated(client, monkeypatch):
    _fake_google(monkeypatch, {"email": "someone@gmail.com", "verified_email": True})
    resp = client.get("/google_authorized", headers={"Accept-Language": "pt-BR"})
    assert 'lang="pt-BR"' in resp.get_data(as_text=True)
    assert "Acesso negado" in resp.get_data(as_text=True)


def test_missing_secret_key_fails_fast(monkeypatch):
    monkeypatch.setattr(Config, "SECRET_KEY", None)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        app_module.check_config()


def test_missing_oauth_credentials_fails_fast(monkeypatch):
    monkeypatch.setattr(Config, "CLIENT_ID", None)
    with pytest.raises(RuntimeError, match="CLIENT_ID"):
        app_module.check_config()


def test_logged_user_email_matches_fixture():
    # sanity check do fixture usado nos outros testes
    assert auth.is_email_allowed(USER["email"])
