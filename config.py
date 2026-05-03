import os
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    cohere_api_key: str = ""
    llm_provider: str = "cohere/command-r-plus"
    search_max_results: int = 5
    db_path: str = "data/research.db"
    crawl_timeout: int = 30000
    flask_host: str = "0.0.0.0"
    flask_port: int = 5000
    flask_debug: bool = True

    class Config:
        env_file = ".env"


settings = Settings()
