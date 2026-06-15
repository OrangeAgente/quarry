"""Stage 1 of agentic collection: turn (agent persona + question) into a
collection plan — a list of requirements (EEIs), each seeded with search queries.
"""
import json
import uuid

from llm import chat_json
from models import Agent, Requirement
from prompt_templates import build_plan_prompt


def build_collection_plan(agent: Agent, mission_id: str, question: str) -> list[Requirement]:
    """Call the LLM to decompose the question into requirements. Returns
    Requirement objects (not yet persisted). Raises ValueError if the model
    produced nothing usable."""
    parsed, _raw = chat_json(agent.persona_prompt, build_plan_prompt(question), max_tokens=1500)

    items = []
    if parsed and isinstance(parsed.get("requirements"), list):
        items = parsed["requirements"]
    if not items:
        raise ValueError("planner returned no requirements")

    requirements: list[Requirement] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        queries = [q.strip() for q in (item.get("queries") or []) if isinstance(q, str) and q.strip()]
        if not queries:
            queries = [f"{question} {title}"]
        requirements.append(Requirement(
            id=str(uuid.uuid4()),
            mission_id=mission_id,
            title=title[:200],
            description=(item.get("description") or "").strip()[:1000],
            rationale=(item.get("rationale") or "").strip()[:1000],
            status="pending",
            attempts=0,
            next_queries_json=json.dumps(queries[:3]),
        ))

    if not requirements:
        raise ValueError("planner returned no usable requirements")
    return requirements
