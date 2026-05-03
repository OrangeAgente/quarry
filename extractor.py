import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

import litellm

from config import settings
from models import Document, ExtractedData

DEFAULT_PROMPT = (
    "Analyze this document and extract the following in JSON format:\n"
    "1. summary: A 2-3 sentence summary of the content\n"
    "2. key_facts: A list of the most important facts or claims\n"
    "3. entities: Named entities (people, organizations, locations, dates)\n"
    "4. topics: Main topics or themes covered\n"
    "5. sentiment: Overall sentiment (positive, negative, neutral, mixed)\n"
)


def extract_from_document(
    doc: Document,
    custom_prompt: str = "",
) -> Optional[ExtractedData]:
    prompt = custom_prompt or DEFAULT_PROMPT

    content = doc.content_fit or doc.content_markdown
    if not content:
        print(f"[EXTRACT] No content for {doc.url}", file=sys.stderr, flush=True)
        return None

    # Truncate to avoid exceeding context limits (~20k chars)
    if len(content) > 20000:
        content = content[:20000] + "\n\n[...truncated...]"

    print(f"[EXTRACT] Starting extraction for {doc.url} ({len(content)} chars)", file=sys.stderr, flush=True)

    messages = [
        {
            "role": "system",
            "content": "You are a data extraction assistant. Extract structured information from web documents. Always respond with valid JSON.",
        },
        {
            "role": "user",
            "content": f"{prompt}\n\n---\nDOCUMENT SOURCE: {doc.url}\n---\n\n{content}",
        },
    ]

    try:
        response = litellm.completion(
            model=settings.llm_provider,
            messages=messages,
            api_key=settings.cohere_api_key,
            temperature=0.0,
            max_tokens=2000,
        )

        result_text = response.choices[0].message.content
        print(f"[EXTRACT] Got response ({len(result_text)} chars)", file=sys.stderr, flush=True)

        # Try to parse as JSON, wrap in object if needed
        try:
            parsed = json.loads(result_text)
            result_json = json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            result_json = json.dumps({"raw_response": result_text}, indent=2)

        return ExtractedData(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            model=settings.llm_provider,
            extracted_at=datetime.now(timezone.utc).isoformat(),
            prompt=prompt[:500],
            data_json=result_json,
        )

    except Exception as e:
        print(f"[EXTRACT] ERROR for {doc.url}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None
