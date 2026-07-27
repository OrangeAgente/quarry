"""Startup reconciliation, mission delete, and the accepted-by-user override."""
import asyncio

import storage
from models import Agent, Mission, Requirement, Document


def _setup(tmp_path):
    storage.DB_PATH = str(tmp_path / "t.db")

    async def go():
        await storage.init_db()
        await storage.insert_agent(Agent(id="a1", name="N", expertise="x",
                                         persona_prompt="p", created_at="t"))
    asyncio.run(go())


def _mission(mid, status):
    return Mission(id=mid, agent_id="a1", question="Q", status=status, created_at="t")


def test_reconcile_marks_only_in_flight_missions(tmp_path):
    _setup(tmp_path)

    async def go():
        for mid, status in [("m_plan", "planning"), ("m_coll", "collecting"),
                            ("m_syn", "synthesizing"), ("m_wait", "awaiting_approval"),
                            ("m_done", "done"), ("m_err", "error")]:
            await storage.insert_mission(_mission(mid, status))

        n = await storage.reconcile_interrupted_missions()
        assert n == 3, "only the three in-flight states are orphaned"

        # In-flight missions are now failed, with an explanation.
        for mid in ("m_plan", "m_coll", "m_syn"):
            m = await storage.get_mission(mid)
            assert m.status == "error"
            assert "restart" in (m.error or "").lower()
            assert m.finished_at

        # awaiting_approval waits on the user, not a thread — must survive.
        assert (await storage.get_mission("m_wait")).status == "awaiting_approval"
        # Terminal states are untouched.
        assert (await storage.get_mission("m_done")).status == "done"

        # Idempotent: a second pass has nothing left to do.
        assert await storage.reconcile_interrupted_missions() == 0

    asyncio.run(go())


def test_delete_mission_keeps_documents(tmp_path):
    _setup(tmp_path)

    async def go():
        await storage.insert_mission(_mission("m1", "done"))
        await storage.insert_requirement(Requirement(id="r1", mission_id="m1", title="T"))
        doc_id = await storage.upsert_document(Document(
            id="d1", url="http://x/1", domain="x", title="t", search_query="Q",
            crawled_at="t", content_markdown="body"))
        await storage.link_mission_document("m1", "r1", doc_id)

        await storage.delete_mission("m1")

        assert await storage.get_mission("m1") is None
        assert await storage.get_requirements_for_mission("m1") == []
        assert await storage.get_mission_documents("m1") == []
        # The crawled page is shared with the library and must survive.
        assert await storage.count_documents() == 1
        assert (await storage.get_document(doc_id)) is not None

    asyncio.run(go())


def test_accepted_by_user_is_recorded_distinctly(tmp_path):
    _setup(tmp_path)

    async def go():
        await storage.insert_mission(_mission("m1", "done"))
        await storage.insert_requirement(Requirement(
            id="r1", mission_id="m1", title="T", status="unmet", attempts=3,
            assessment_missing="no coverage", assessment_confidence="low"))

        await storage.update_requirement("r1", status="satisfied", accepted_by_user=1)
        r = (await storage.get_requirements_for_mission("m1"))[0]
        assert r.status == "satisfied"
        # The override is distinguishable from an assessor judgement, so the UI
        # never implies the assessor was satisfied.
        assert r.accepted_by_user == 1
        assert r.assessment_confidence == "low"

        # A re-task clears the override and reopens the requirement.
        await storage.update_requirement("r1", status="pending", attempts=0,
                                         accepted_by_user=0, assessment_missing="")
        r = (await storage.get_requirements_for_mission("m1"))[0]
        assert (r.status, r.attempts, r.accepted_by_user) == ("pending", 0, 0)

    asyncio.run(go())
