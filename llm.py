"""Thin wrapper around LiteLLM, shared by the agentic-collection modules.

Mirrors the call pattern in extractor.py (model = settings.llm_provider, the
Cohere key passed explicitly, JSON-with-fallback parsing) so there is one place
that knows how we talk to the LLM.
"""
import json
import re
import sys
from typing import Optional

import litellm

from config import settings


def model_for(tier: str = "reasoning") -> str:
    """Resolve the model id for a tier. 'fast' uses llm_provider_fast when set
    (e.g. a local Ollama model), otherwise everything falls back to
    llm_provider."""
    if tier == "fast" and settings.llm_provider_fast:
        return settings.llm_provider_fast
    return settings.llm_provider


def _provider_kwargs(model: str) -> dict:
    """litellm kwargs that depend on the provider: Ollama needs api_base and no
    key; hosted providers (Cohere, etc.) get the key."""
    if model.startswith(("ollama/", "ollama_chat/")):
        return {"api_base": settings.ollama_api_base}
    return {"api_key": settings.cohere_api_key}


def _complete(model: str, system: str, user: str, temperature: float, max_tokens: int) -> str:
    response = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        **_provider_kwargs(model),
    )
    return response.choices[0].message.content or ""


def chat_ex(system: str, user: str, temperature: float = 0.0,
            max_tokens: int = 2000, tier: str = "reasoning",
            allow_fallback: bool = True) -> tuple[str, str]:
    """Return (assistant text, model that actually answered). If the 'fast'
    tier fails (e.g. local Ollama is down), fall back once to the reasoning
    model so brief/extraction still succeed instead of silently degrading.
    Raises if that also fails."""
    model = model_for(tier)
    try:
        return _complete(model, system, user, temperature, max_tokens), model
    except Exception as e:  # noqa: BLE001
        reasoning = model_for("reasoning")
        if allow_fallback and tier == "fast" and model != reasoning:
            print(f"[LLM] fast tier ({model}) failed ({type(e).__name__}); "
                  f"falling back to {reasoning}", file=sys.stderr, flush=True)
            return _complete(reasoning, system, user, temperature, max_tokens), reasoning
        raise


def chat(system: str, user: str, temperature: float = 0.0,
         max_tokens: int = 2000, tier: str = "reasoning",
         allow_fallback: bool = True) -> str:
    """chat_ex, discarding the model name."""
    text, _model = chat_ex(system, user, temperature=temperature,
                           max_tokens=max_tokens, tier=tier,
                           allow_fallback=allow_fallback)
    return text


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort parse of a JSON object from model output (handles ```json
    fences and leading/trailing prose)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip code fences
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Grab the outermost {...}
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return None


def chat_json(system: str, user: str, temperature: float = 0.0,
              max_tokens: int = 2000, tier: str = "reasoning"):
    """Return (parsed_dict_or_None, raw_text)."""
    raw = chat(system, user, temperature=temperature, max_tokens=max_tokens, tier=tier)
    parsed = _extract_json(raw)
    if parsed is None:
        print(f"[LLM] could not parse JSON from response: {raw[:200]!r}",
              file=sys.stderr, flush=True)
    return parsed, raw
