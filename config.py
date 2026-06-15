import os
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
