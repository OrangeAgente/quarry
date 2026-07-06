import json
import os
import sys
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    cohere_api_key: str = ""
    llm_provider: str = "cohere/command-a-03-2025"
    # Optional "fast" tier for summarization-style calls (brief, extraction).
    # Empty -> those calls fall back to llm_provider. For a local Ollama model
    # use e.g. "ollama_chat/qwen2.5:14b" with ollama_api_base set.
    llm_provider_fast: str = ""
    ollama_api_base: str = "http://host.docker.internal:11434"
    search_max_results: int = 5
    db_path: str = "data/research.db"
    crawl_timeout: int = 30000
    flask_host: str = "0.0.0.0"
    flask_port: int = 5000
    flask_debug: bool = False
    flask_secret_key: str = ""

    class Config:
        env_file = ".env"


settings = Settings()


# --- UI-editable overrides (Settings page) ---
# Persisted as JSON in the data volume so they survive container recreate
# (unlike the baked .env), layered over the env-derived defaults at startup,
# and mutated live on save. These take precedence over .env.
OVERRIDE_KEYS = (
    "llm_provider", "llm_provider_fast", "ollama_api_base",
    "cohere_api_key", "search_max_results",
)

# Seed suggestions for the Settings model dropdowns; user-used models are added.
DEFAULT_KNOWN_MODELS = [
    "cohere/command-a-03-2025",
    "ollama_chat/qwen2.5:14b",
    "anthropic/claude-sonnet-4-5",
    "openai/gpt-4o-mini",
]


def _overrides_path() -> str:
    data_dir = os.path.dirname(settings.db_path) or "."
    return os.path.join(data_dir, "settings.json")


def load_overrides() -> None:
    path = _overrides_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except json.JSONDecodeError as e:
        # Don't silently fall back to .env defaults — a truncated file would
        # otherwise make UI-saved settings (incl. an API key) vanish unnoticed.
        print(f"[CONFIG] WARNING: {path} is corrupt ({e}); ignoring saved "
              f"settings and preserving the file as {path}.corrupt",
              file=sys.stderr, flush=True)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return
    for k in OVERRIDE_KEYS:
        if data.get(k) is not None:
            setattr(settings, k, data[k])


def save_overrides(values: dict) -> None:
    path = _overrides_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            current = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        current = {}
    for k in OVERRIDE_KEYS:
        if k in values and values[k] is not None:
            current[k] = values[k]
            setattr(settings, k, values[k])
    # Remember every model that's been used so it stays in the dropdown even
    # after you overwrite a field.
    models = list(current.get("known_models", []))
    for m in (current.get("llm_provider"), current.get("llm_provider_fast")):
        if m and m not in models:
            models.append(m)
    current["known_models"] = models
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Atomic write (temp file + rename) so a crash mid-dump can't truncate the
    # live file and take UI-saved settings (incl. the API key) with it.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)  # may hold an API key
    except OSError:
        pass  # best-effort (e.g. Windows bind mounts)
    os.replace(tmp, path)


def known_models() -> list[str]:
    """Model ids to suggest in the Settings dropdowns: seeds + any ever used +
    the currently active ones."""
    models = list(DEFAULT_KNOWN_MODELS)
    try:
        with open(_overrides_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    for m in list(data.get("known_models", [])) + [settings.llm_provider, settings.llm_provider_fast]:
        if m and m not in models:
            models.append(m)
    return models


load_overrides()
