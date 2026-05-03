import os
import json
import aiosqlite
from typing import Optional
from models import Document, ExtractedData, SearchRecord

DB_PATH = None


def get_db_path() -> str:
    global DB_PATH
    if DB_PATH is None:
        from config import settings
        DB_PATH = settings.db_path
    return DB_PATH


async def init_db():
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                domain TEXT NOT NULL,
                title TEXT,
                search_query TEXT,
                crawled_at TEXT NOT NULL,
                content_markdown TEXT,
                content_fit TEXT,
                word_count INTEGER DEFAULT 0,
                links_internal INTEGER DEFAULT 0,
                links_external INTEGER DEFAULT 0,
                metadata_json TEXT,
                UNIQUE(url, search_query)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS extractions (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(id),
                model TEXT,
                extracted_at TEXT NOT NULL,
                prompt TEXT,
                data_json TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                executed_at TEXT NOT NULL,
                result_count INTEGER DEFAULT 0,
                job_id TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE searches ADD COLUMN job_id TEXT")
        except Exception:
            pass
        await db.commit()


async def insert_document(doc: Document) -> bool:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        try:
            await db.execute(
                """INSERT OR REPLACE INTO documents
                   (id, url, domain, title, search_query, crawled_at,
                    content_markdown, content_fit, word_count,
                    links_internal, links_external, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc.id, doc.url, doc.domain, doc.title, doc.search_query,
                 doc.crawled_at, doc.content_markdown, doc.content_fit,
                 doc.word_count, doc.links_internal, doc.links_external,
                 doc.metadata_json)
            )
            await db.commit()
            return True
        except Exception as e:
            print(f"Error inserting document: {e}")
            return False


async def insert_extraction(ext: ExtractedData) -> bool:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        try:
            await db.execute(
                """INSERT INTO extractions
                   (id, document_id, model, extracted_at, prompt, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ext.id, ext.document_id, ext.model, ext.extracted_at,
                 ext.prompt, ext.data_json)
            )
            await db.commit()
            return True
        except Exception as e:
            print(f"Error inserting extraction: {e}")
            return False


async def insert_search(record: SearchRecord) -> bool:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        try:
            await db.execute(
                """INSERT INTO searches (id, query, executed_at, result_count, job_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (record.id, record.query, record.executed_at, record.result_count, record.job_id)
            )
            await db.commit()
            return True
        except Exception as e:
            print(f"Error inserting search: {e}")
            return False


async def get_document(doc_id: str) -> Optional[Document]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return Document(**dict(row))
    return None


async def get_documents_by_search(query: str) -> list[Document]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM documents WHERE search_query = ? ORDER BY crawled_at DESC",
            (query,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [Document(**dict(row)) for row in rows]


async def get_all_documents() -> list[Document]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM documents ORDER BY crawled_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [Document(**dict(row)) for row in rows]


async def get_extractions_for_document(doc_id: str) -> list[ExtractedData]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM extractions WHERE document_id = ? ORDER BY extracted_at DESC",
            (doc_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [ExtractedData(**dict(row)) for row in rows]


async def get_search_history() -> list[SearchRecord]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM searches ORDER BY executed_at DESC LIMIT 50") as cursor:
            rows = await cursor.fetchall()
            return [SearchRecord(**dict(row)) for row in rows]


async def count_documents() -> int:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM documents") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def count_searches() -> int:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM searches") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def count_extractions() -> int:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM extractions") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def count_domains() -> int:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(DISTINCT domain) FROM documents") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_doc_ids_with_extractions() -> set[str]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT DISTINCT document_id FROM extractions") as cursor:
            rows = await cursor.fetchall()
            return {row[0] for row in rows}


async def get_search_history_enriched() -> list[dict]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.id, s.query, s.executed_at, s.result_count, s.job_id,
                      (SELECT COUNT(DISTINCT d.id)
                       FROM documents d
                       JOIN extractions e ON e.document_id = d.id
                       WHERE d.search_query = s.query) AS extracted_count
               FROM searches s
               ORDER BY s.executed_at DESC
               LIMIT 50"""
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_related_documents(doc_id: str, search_query: str, domain: str, limit: int = 3) -> list[Document]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM documents
               WHERE id != ?
                 AND (search_query = ? OR domain = ?)
               ORDER BY CASE WHEN search_query = ? THEN 0 ELSE 1 END, crawled_at DESC
               LIMIT ?""",
            (doc_id, search_query, domain, search_query, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [Document(**dict(row)) for row in rows]
