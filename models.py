from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str


class Document(BaseModel):
    id: str
    url: str
    domain: str
    title: Optional[str] = None
    search_query: str
    crawled_at: str
    content_markdown: Optional[str] = None
    content_fit: Optional[str] = None
    word_count: int = 0
    links_internal: int = 0
    links_external: int = 0
    metadata_json: Optional[str] = None


class ExtractedData(BaseModel):
    id: str
    document_id: str
    model: str
    extracted_at: str
    prompt: str
    data_json: str


class SearchRecord(BaseModel):
    id: str
    query: str
    executed_at: str
    result_count: int
    job_id: Optional[str] = None
