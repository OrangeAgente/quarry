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
    # Engines tried in order until one returns results (ddgs backends).
    # The duckduckgo backend alone frequently returns nothing under load.
    search_backends: str = "auto,brave,bing,duckduckgo"
    db_path: str = "data/research.db"
    crawl_timeout: int = 30000
    flask_host: str = "0.0.0.0"
    flask_port: int = 5000
    flask_debug: bool = False
    flask_secret_key: str = ""
    # Optional login password. Empty = auth disabled (localhost-only posture).
    # Accepts plaintext or a Werkzeug hash (pbkdf2:/scrypt: prefix).
    # Deliberately NOT in OVERRIDE_KEYS: the Settings page must never be able
    # to set or clear it, or an unauthenticated visitor could.
    quarry_password: str = ""

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


def persistent_secret_key() -> str:
    """A stable Flask secret key. Without one, sessions die on every restart —
    tolerable for flash messages, unacceptable once login sessions exist.
    Precedence: FLASK_SECRET_KEY env var, else a key generated once and kept in
    the data volume (0600)."""
    if settings.flask_secret_key:
        return settings.flask_secret_key
    import secrets as _secrets
    path = os.path.join(os.path.dirname(settings.db_path) or ".", "secret_key")
    try:
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    key = _secrets.token_hex(32)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return key


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
