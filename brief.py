"""Final stage: synthesize a Markdown brief from a mission's requirements and
collected documents, with [n] citations. Supports a delta block ("what's new
since last run") for scheduled runs.
"""
from models import Document, Mission, Requirement
from llm import chat
from prompt_templates import build_brief_prompt


def _coverage_block(requirements: list[Requirement]) -> str:
    lines = []
    for r in requirements:
        mark = {"satisfied": "[x]", "unmet": "[ ] UNMET", "pending": "[ ] pending"}.get(r.status, r.status)
        lines.append(f"- {mark} {r.title}")
    return "\n".join(lines) or "(no requirements)"


def _sources_block(docs: list[Document], max_docs: int = 30, excerpt_chars: int = 800) -> str:
    lines = []
    for i, d in enumerate(docs[:max_docs], 1):
        body = (d.content_fit or d.content_markdown or "")[:excerpt_chars]
        lines.append(f"[{i}] {d.title or d.domain} — {d.url}\n{body}")
    return "\n\n".join(lines)


def _delta_block(docs: list[Document], new_urls: set[str], max_items: int = 10) -> str:
    if not new_urls:
        return ""
    fresh = [d for d in docs if d.url in new_urls][:max_items]
    if not fresh:
        return ""
    lines = ["NEW SINCE LAST RUN (lead the brief with these):"]
    for d in fresh:
        lines.append(f"- {d.title or d.domain} ({d.url})")
    return "\n".join(lines)


def synthesize_brief(
    mission: Mission,
    requirements: list[Requirement],
    docs: list[Document],
    new_urls: set[str] | None = None,
) -> str:
    """Return Markdown. On LLM failure, return a minimal fallback brief built
    from coverage so a run always produces something readable."""
    coverage = _coverage_block(requirements)
    delta = _delta_block(docs, new_urls or set())
    prompt = build_brief_prompt(mission.question, coverage, _sources_block(docs), delta)
    persona = "You are an expert analyst writing a concise, source-grounded research brief."
    try:
        return chat(persona, prompt, max_tokens=2000, tier="fast")
    except Exception as e:  # noqa: BLE001 - brief must degrade gracefully
        n_sat = sum(1 for r in requirements if r.status == "satisfied")
        lines = [
            f"## Summary",
            f"_Automated brief generation failed ({type(e).__name__}); showing coverage only._",
            "",
            f"Collected {len(docs)} sources. Requirement coverage "
            f"{n_sat}/{len(requirements)} satisfied.",
            "",
            "## Coverage & Gaps",
            coverage,
        ]
        return "\n".join(lines)
