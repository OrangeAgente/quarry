import pytest

import llm


def test_fast_falls_back_to_reasoning(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_provider", "cohere/command-a-03-2025")
    monkeypatch.setattr(llm.settings, "llm_provider_fast", "ollama_chat/qwen2.5:14b")

    def fake_complete(model, system, user, temperature, max_tokens):
        if model.startswith("ollama"):
            raise RuntimeError("Connection refused")
        return "from-reasoning"

    monkeypatch.setattr(llm, "_complete", fake_complete)
    # Fast tier fails -> transparently uses the reasoning model.
    assert llm.chat("s", "u", tier="fast") == "from-reasoning"


def test_reasoning_failure_raises(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_provider", "cohere/command-a-03-2025")
    monkeypatch.setattr(llm.settings, "llm_provider_fast", "ollama_chat/qwen2.5:14b")

    def boom(model, system, user, temperature, max_tokens):
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "_complete", boom)
    with pytest.raises(RuntimeError):
        llm.chat("s", "u", tier="reasoning")


def test_chat_ex_reports_model_that_answered(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_provider", "cohere/command-a-03-2025")
    monkeypatch.setattr(llm.settings, "llm_provider_fast", "ollama_chat/qwen2.5:14b")

    def fake_complete(model, system, user, temperature, max_tokens):
        if model.startswith("ollama"):
            raise RuntimeError("Connection refused")
        return "text"

    monkeypatch.setattr(llm, "_complete", fake_complete)
    text, model = llm.chat_ex("s", "u", tier="fast")
    assert text == "text"
    assert model == "cohere/command-a-03-2025"  # the fallback, not the intended fast model

    # And when the fast tier works, it reports the fast model.
    monkeypatch.setattr(llm, "_complete", lambda m, s, u, t, mt: "ok")
    _, model = llm.chat_ex("s", "u", tier="fast")
    assert model == "ollama_chat/qwen2.5:14b"


def test_no_fallback_when_fast_equals_reasoning(monkeypatch):
    # If no distinct fast model is set, a failure must not loop/duplicate.
    monkeypatch.setattr(llm.settings, "llm_provider", "cohere/command-a-03-2025")
    monkeypatch.setattr(llm.settings, "llm_provider_fast", "")
    calls = []

    def once(model, system, user, temperature, max_tokens):
        calls.append(model)
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "_complete", once)
    with pytest.raises(RuntimeError):
        llm.chat("s", "u", tier="fast")
    assert len(calls) == 1
