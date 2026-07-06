"""The origin-check CSRF guard: cross-site browser POSTs are rejected, while
same-origin form posts and non-browser clients pass through."""
import storage


def _client(tmp_path):
    storage.DB_PATH = str(tmp_path / "t.db")
    import app as app_mod
    return app_mod.app.test_client()


def test_cross_site_origin_blocked(tmp_path):
    client = _client(tmp_path)
    r = client.post("/search", data={"query": "x"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_cross_site_fetch_metadata_blocked(tmp_path):
    client = _client(tmp_path)
    r = client.post("/search", data={"query": "x"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_and_headerless_posts_pass(tmp_path):
    client = _client(tmp_path)
    # Non-browser client (no Origin / Sec-Fetch-Site): passes the guard,
    # reaches the route (unknown agent -> redirect, not 403).
    r = client.post("/agents/nonexistent/run", data={"question": "q"})
    assert r.status_code == 302
    # Same-origin browser post: Origin matches Host.
    r = client.post("/agents/nonexistent/run", data={"question": "q"},
                    headers={"Origin": "http://localhost",
                             "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 302
    # GETs are never blocked.
    assert client.get("/").status_code == 200
