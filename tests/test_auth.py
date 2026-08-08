"""The optional password gate: off by default, airtight when on."""
import pytest

import auth
import storage


@pytest.fixture()
def client(tmp_path, monkeypatch):
    storage.DB_PATH = str(tmp_path / "t.db")
    monkeypatch.setattr(auth.settings, "db_path", str(tmp_path / "t.db"))
    import app as app_mod
    # The Flask app is module-global across test files; force ensure_db to
    # re-run against this test's fresh temp DB.
    app_mod.app._db_initialized = False
    auth._failures.clear()
    return app_mod.app.test_client()


def _enable(monkeypatch, password="hunter2-quarry"):
    monkeypatch.setattr(auth.settings, "quarry_password", password)


def test_auth_off_means_open(client, monkeypatch):
    monkeypatch.setattr(auth.settings, "quarry_password", "")
    assert client.get("/").status_code == 200
    assert client.get("/agents").status_code == 200
    # /login just bounces home when auth is off
    r = client.get("/login")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def test_auth_on_gates_everything(client, monkeypatch):
    _enable(monkeypatch)
    for path in ("/", "/agents", "/missions", "/settings", "/documents", "/history"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert "/login" in r.headers["Location"], path
    # APIs answer 401 JSON, not a redirect the fetch() would silently follow
    r = client.get("/api/mission/nope")
    assert r.status_code == 401
    # POST routes are gated too
    r = client.post("/search", data={"query": "x"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_login_page_is_reachable_and_standalone(client, monkeypatch):
    _enable(monkeypatch)
    r = client.get("/login")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # Must not leak the workspace shell (doc counts, recent queries, models)
    assert "sidebar" not in html
    assert "credits-card" not in html


def test_wrong_password_rejected_right_password_admits(client, monkeypatch):
    _enable(monkeypatch)
    r = client.post("/login", data={"password": "wrong"})
    assert r.status_code == 401
    r = client.post("/login", data={"password": "hunter2-quarry"})
    assert r.status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/api/mission/nope").status_code == 404  # authed: real 404 now


def test_hashed_password_supported(client, monkeypatch):
    from werkzeug.security import generate_password_hash
    _enable(monkeypatch, generate_password_hash("s3cret"))
    assert client.post("/login", data={"password": "s3cret"}).status_code == 302
    client.post("/logout")
    assert client.post("/login", data={"password": "wrong"}).status_code == 401


def test_rate_limit_locks_out(client, monkeypatch):
    _enable(monkeypatch)
    for _ in range(auth._MAX_FAILURES):
        client.post("/login", data={"password": "wrong"})
    r = client.post("/login", data={"password": "hunter2-quarry"})  # even correct
    assert r.status_code == 429


def test_logout_ends_the_session(client, monkeypatch):
    _enable(monkeypatch)
    client.post("/login", data={"password": "hunter2-quarry"})
    assert client.get("/").status_code == 200
    client.post("/logout")
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_no_open_redirect(client, monkeypatch):
    _enable(monkeypatch)
    r = client.post("/login", data={"password": "hunter2-quarry",
                                    "next": "https://evil.example/phish"})
    assert r.status_code == 302
    assert "evil.example" not in r.headers["Location"]
    client.post("/logout")
    r = client.post("/login", data={"password": "hunter2-quarry", "next": "//evil.example"})
    assert "evil.example" not in r.headers["Location"]


def test_insecure_exposure_flag(client, monkeypatch):
    import app as app_mod
    # exposed + no password -> flagged
    monkeypatch.setattr(auth.settings, "quarry_bind", "0.0.0.0")
    monkeypatch.setattr(auth.settings, "quarry_password", "")
    assert app_mod.insecure_exposure() is True
    # exposed + password -> fine
    monkeypatch.setattr(auth.settings, "quarry_password", "pw")
    assert app_mod.insecure_exposure() is False
    # loopback + no password -> fine (the default posture)
    monkeypatch.setattr(auth.settings, "quarry_password", "")
    monkeypatch.setattr(auth.settings, "quarry_bind", "127.0.0.1")
    assert app_mod.insecure_exposure() is False


def test_security_headers_present(client, monkeypatch):
    monkeypatch.setattr(auth.settings, "quarry_password", "")
    r = client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
