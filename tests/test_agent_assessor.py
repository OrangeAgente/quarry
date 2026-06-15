import agent_assessor
from models import Requirement


def _req():
    return Requirement(id="r", mission_id="m", title="T", description="D")


def test_satisfied(monkeypatch):
    monkeypatch.setattr(agent_assessor, "chat_json", lambda s, u, **k: (
        {"satisfied": True, "confidence": "high", "missing": "", "next_queries": []}, "raw"))
    a = agent_assessor.assess_requirement(_req(), [])
    assert a.satisfied and a.confidence == "high"


def test_unsatisfied_with_next_queries(monkeypatch):
    monkeypatch.setattr(agent_assessor, "chat_json", lambda s, u, **k: (
        {"satisfied": False, "confidence": "low", "missing": "x", "next_queries": ["nq1", "nq2"]}, "raw"))
    a = agent_assessor.assess_requirement(_req(), [])
    assert not a.satisfied
    assert a.next_queries == ["nq1", "nq2"]


def test_parse_failure_is_not_satisfied(monkeypatch):
    monkeypatch.setattr(agent_assessor, "chat_json", lambda s, u, **k: (None, "raw"))
    a = agent_assessor.assess_requirement(_req(), [])
    assert not a.satisfied
    assert a.next_queries == []
