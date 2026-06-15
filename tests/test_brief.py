import brief
from models import Mission, Requirement, Document


def _mission():
    return Mission(id="m", agent_id="a", question="Q", created_at="t")


def _req(status):
    return Requirement(id="r" + status, mission_id="m", title="T-" + status, status=status)


def _doc(url):
    return Document(id=url, url=url, domain="d", title="t", search_query="Q", crawled_at="t",
                    content_markdown="body text")


def test_brief_includes_delta_when_new_urls(monkeypatch):
    captured = {}

    def fake_chat(persona, prompt, **k):
        captured["prompt"] = prompt
        return "## Summary\nanswer [1]"

    monkeypatch.setattr(brief, "chat", fake_chat)
    out = brief.synthesize_brief(_mission(), [_req("satisfied"), _req("unmet")], [_doc("u1")], {"u1"})
    assert "answer" in out
    assert "NEW SINCE LAST RUN" in captured["prompt"]


def test_brief_no_delta_when_empty(monkeypatch):
    captured = {}
    monkeypatch.setattr(brief, "chat", lambda p, prompt, **k: captured.setdefault("prompt", prompt) or "ok")
    brief.synthesize_brief(_mission(), [_req("satisfied")], [_doc("u1")], set())
    assert "NEW SINCE LAST RUN" not in captured["prompt"]


def test_brief_fallback_on_llm_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(brief, "chat", boom)
    out = brief.synthesize_brief(_mission(), [_req("satisfied")], [_doc("u1")], set())
    assert "Coverage & Gaps" in out
    assert "1/1" in out
