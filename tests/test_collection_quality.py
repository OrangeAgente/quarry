"""Search fallback, junk-page rejection, and cron validation."""
import content_quality
import search
from models import Document


# ---------- junk detection ----------

def test_block_pages_rejected():
    for title in ("Checking your browser - reCAPTCHA", "Just a moment...",
                  "Access Denied", "Attention Required! | Cloudflare"):
        junk, why = content_quality.looks_like_block_page(title, 5000)
        assert junk, f"{title!r} should be rejected"
        assert why


def test_thin_pages_rejected():
    junk, why = content_quality.looks_like_block_page("Real Article", 12)
    assert junk and "12 words" in why


def test_real_pages_kept():
    junk, _ = content_quality.looks_like_block_page("Solar cell - Wikipedia", 9000)
    assert not junk


def test_is_usable_matches_document_shape():
    good = Document(id="1", url="u", domain="d", title="Real Page",
                    search_query="q", crawled_at="t", word_count=900)
    bad = Document(id="2", url="u", domain="d", title="Checking your browser",
                   search_query="q", crawled_at="t", word_count=18)
    assert content_quality.is_usable(good)
    assert not content_quality.is_usable(bad)


# ---------- search fallback ----------

def _row(u):
    return {"href": u, "title": "t", "body": "b"}


def test_falls_through_to_a_working_engine(monkeypatch):
    monkeypatch.setattr(search.settings, "search_backends", "auto,brave,bing")
    tried = []

    def fake_rows(query, max_results, backend):
        tried.append(backend)
        if backend == "auto":
            raise search.RatelimitException("throttled")
        if backend == "brave":
            return []                      # answered, but empty
        return [_row("http://ok/1")]       # bing succeeds

    monkeypatch.setattr(search, "_rows", fake_rows)
    out = search.web_search("q", 3)
    assert [r.url for r in out] == ["http://ok/1"]
    assert tried[0] == "auto" and tried[-1] == "bing"


def test_returns_empty_when_all_engines_fail(monkeypatch):
    monkeypatch.setattr(search.settings, "search_backends", "auto,brave")

    def boom(query, max_results, backend):
        raise RuntimeError("engine down")

    monkeypatch.setattr(search, "_rows", boom)
    assert search.web_search("q", 3) == []


def test_first_engine_short_circuits(monkeypatch):
    monkeypatch.setattr(search.settings, "search_backends", "auto,brave,bing")
    tried = []

    def fake_rows(query, max_results, backend):
        tried.append(backend)
        return [_row("http://a/1")]

    monkeypatch.setattr(search, "_rows", fake_rows)
    search.web_search("q", 3)
    assert tried == ["auto"], "must not query further engines once one answers"


def test_rows_without_urls_are_dropped(monkeypatch):
    monkeypatch.setattr(search.settings, "search_backends", "auto")
    monkeypatch.setattr(search, "_rows",
                        lambda q, m, b: [{"title": "no url"}, _row("http://a/1")])
    assert [r.url for r in search.web_search("q", 3)] == ["http://a/1"]


# ---------- cron validation ----------

def test_cron_validation():
    import scheduler
    assert scheduler.validate_cron("0 7 * * *")[0]
    assert scheduler.validate_cron("")[0], "blank means unscheduled, not invalid"
    assert not scheduler.validate_cron("not a cron")[0]
    assert not scheduler.validate_cron("99 99 * * *")[0]
    assert scheduler.describe_next_run("0 7 * * *")
    assert scheduler.describe_next_run("") == ""
