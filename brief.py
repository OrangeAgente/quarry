"""Final stage: synthesize a Markdown brief from a mission's requirements and
collected documents, with [n] citations. Supports a delta block ("what's new
since last run") for scheduled runs.
"""
import re

from models import Document, Mission, Requirement
from llm import chat
from content_quality import is_usable
from prompt_templates import build_brief_prompt


def _coverage_block(requirements: list[Requirement]) -> str:
    lines = []
    for r in requirements:
        mark = {"satisfied": "[x]", "unmet": "[ ] UNMET", "pending": "[ ] pending"}.get(r.status, r.status)
        lines.append(f"- {mark} {r.title}")
    return "\n".join(lines) or "(no requirements)"


MAX_BRIEF_SOURCES = 20


def ordered_sources(docs: list[Document], max_docs: int = MAX_BRIEF_SOURCES) -> list[Document]:
    """The exact source ordering the brief's `[n]` markers refer to: usable
    (non-junk) docs first, never dropping one while slots remain, capped.
    The mission page numbers its source rail with this so citations bind to the
    right document."""
    return sorted(docs, key=lambda d: not is_usable(d))[:max_docs]


def _sources_block(docs: list[Document], max_docs: int = MAX_BRIEF_SOURCES,
                   excerpt_chars: int = 2200) -> str:
    lines = []
    for i, d in enumerate(ordered_sources(docs, max_docs), 1):
        body = (d.content_fit or d.content_markdown or "")[:excerpt_chars]
        lines.append(f"[{i}] {d.title or d.domain} — {d.url}\n{body}")
    return "\n\n".join(lines)


_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def linkify_citations(html: str, max_n: int) -> str:
    """Turn `[n]` markers in already-sanitized brief HTML into citation
    controls. Runs AFTER markdown_render.render_markdown (never instead of it):
    the only thing injected is markup built from an integer we re-serialize
    ourselves, so no LLM/page content reaches the DOM unescaped. Out-of-range
    numbers are left as plain text."""
    def repl(m: "re.Match[str]") -> str:
        n = int(m.group(1))
        if not 1 <= n <= max_n:
            return m.group(0)
        return (f'<button type="button" class="cite" data-cite="{n}" '
                f'aria-label="Source {n}">{n}</button>')
    return _CITE_RE.sub(repl, html or "")


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
