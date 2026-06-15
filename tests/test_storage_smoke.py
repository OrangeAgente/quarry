import asyncio

import storage
from models import Agent, Mission, Requirement, Document


def test_storage_roundtrip(tmp_path):
    storage.DB_PATH = str(tmp_path / "t.db")

    async def run():
        await storage.init_db()

        # Agents
        await storage.insert_agent(Agent(
            id="a1", name="N", expertise="x", persona_prompt="p", created_at="t"))
        assert (await storage.get_agent("a1")).name == "N"
        assert len(await storage.list_agents()) == 1

        # Missions
        await storage.insert_mission(Mission(
            id="m1", agent_id="a1", question="Q", status="planning", created_at="t"))
        await storage.update_mission("m1", status="done", finished_at="t2")
        assert (await storage.get_mission("m1")).status == "done"
        assert len(await storage.list_missions()) == 1

        # Requirements
        await storage.insert_requirement(Requirement(
            id="r1", mission_id="m1", title="T", status="pending"))
        await storage.update_requirement("r1", status="satisfied", attempts=2)
        reqs = await storage.get_requirements_for_mission("m1")
        assert reqs[0].status == "satisfied" and reqs[0].attempts == 2

        # upsert_document reuses the id for the same (url, search_query)
        id1 = await storage.upsert_document(Document(
            id="d1", url="http://x/1", domain="x", title="t1",
            search_query="Q", crawled_at="t", content_markdown="hello world"))
        id2 = await storage.upsert_document(Document(
            id="dDIFFERENT", url="http://x/1", domain="x", title="t2",
            search_query="Q", crawled_at="t", content_markdown="updated body"))
        assert id1 == id2

        # Join table + lookups
        await storage.link_mission_document("m1", "r1", id1)
        md = await storage.get_mission_documents("m1")
        assert len(md) == 1 and md[0].title == "t2"  # content refreshed in place
        assert len(await storage.get_requirement_documents("m1", "r1")) == 1

        # Delta helpers
        assert await storage.get_prior_mission_urls("m1") == set()
        assert await storage.get_latest_finished_mission("a1", "Q", "m1") is None

        # FTS stayed consistent with the single upserted doc
        assert await storage.count_documents() == 1
        hits = await storage.search_documents_fts("updated")
        assert len(hits) == 1 and hits[0].url == "http://x/1"

    asyncio.run(run())
