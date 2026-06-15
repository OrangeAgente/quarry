"""Gap analysis: judge whether the documents collected for a requirement
satisfy it, and if not, propose refined queries aimed at the gap.
"""
from dataclasses import dataclass

from llm import chat_json
from models import Document, Requirement
from prompt_templates import build_assess_prompt


@dataclass
class Assessment:
    satisfied: bool
    confidence: str
    missing: str
    next_queries: list[str]


def _sources_block(docs: list[Document], max_docs: int = 6, excerpt_chars: int = 2000) -> str:
    lines = []
    for i, d in enumerate(docs[:max_docs], 1):
        body = (d.content_fit or d.content_markdown or "")[:excerpt_chars]
        lines.append(f"[{i}] {d.title or d.domain} ({d.url})\n{body}")
    return "\n\n".join(lines)


def assess_requirement(requirement: Requirement, docs: list[Document]) -> Assessment:
    """Returns an Assessment. On LLM/parse failure, returns a not-satisfied
    assessment with no new queries (caller's attempt cap will still advance)."""
    prompt = build_assess_prompt(requirement.title, requirement.description, _sources_block(docs))
    # The persona isn't needed for grading; a tight system message keeps it cheap.
    parsed, _raw = chat_json(
        "You are a meticulous research analyst grading source coverage.",
        prompt, max_tokens=600,
    )
    if not parsed:
        return Assessment(False, "low", "could not assess", [])

    next_q = [q.strip() for q in (parsed.get("next_queries") or [])
              if isinstance(q, str) and q.strip()]
    return Assessment(
        satisfied=bool(parsed.get("satisfied")),
        confidence=str(parsed.get("confidence") or "low"),
        missing=str(parsed.get("missing") or ""),
        next_queries=next_q[:3],
    )
