import os
import json
import aiosqlite
from typing import Optional
from models import Document, ExtractedData, SearchRecord, Agent, Mission, Requirement

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
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                doc_id UNINDEXED,
                title,
                domain,
                url,
                content,
                tokenize='porter unicode61'
            )
        """)
        # --- Agentic collection tables ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                expertise TEXT,
                persona_prompt TEXT,
                default_max_passes INTEGER DEFAULT 4,
                default_max_sources INTEGER DEFAULT 30,
                default_per_req_attempts INTEGER DEFAULT 3,
                schedule_cron TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES agents(id),
                question TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_json TEXT,
                budget_json TEXT,
                brief_markdown TEXT,
                job_id TEXT,
                parent_mission_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS requirements (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL REFERENCES missions(id),
                title TEXT NOT NULL,
                description TEXT,
                rationale TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                next_queries_json TEXT,
                satisfied_doc_ids_json TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mission_documents (
                mission_id TEXT NOT NULL,
                requirement_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                PRIMARY KEY (mission_id, requirement_id, document_id)
            )
        """)

        async with db.execute("SELECT COUNT(*) FROM documents") as c:
            docs_ct = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM documents_fts") as c:
            fts_ct = (await c.fetchone())[0]
        if fts_ct != docs_ct:
            await db.execute("DELETE FROM documents_fts")
            await db.execute("""
                INSERT INTO documents_fts(doc_id, title, domain, url, content)
                SELECT id, COALESCE(title, ''), domain, url, COALESCE(content_markdown, '')
                FROM documents
            """)
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
            await db.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc.id,))
            await db.execute(
                """INSERT INTO documents_fts(doc_id, title, domain, url, content)
                   VALUES (?, ?, ?, ?, ?)""",
                (doc.id, doc.title or "", doc.domain, doc.url, doc.content_markdown or "")
            )
            await db.commit()
            return True
        except Exception as e:
            print(f"Error inserting document: {e}")
            return False


def _build_fts_query(text: str) -> str:
    parts = []
    for tok in text.split():
        tok = tok.replace('"', '""')
        if tok:
            parts.append(f'"{tok}"')
    return " ".join(parts)


async def search_documents_fts(query: str, search_filter: Optional[str] = None) -> list[Document]:
    fts_q = _build_fts_query(query)
    if not fts_q:
        return []
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if search_filter:
            sql = """SELECT d.* FROM documents d
                     JOIN documents_fts f ON f.doc_id = d.id
                     WHERE documents_fts MATCH ? AND d.search_query = ?
                     ORDER BY rank LIMIT 100"""
            args = (fts_q, search_filter)
        else:
            sql = """SELECT d.* FROM documents d
                     JOIN documents_fts f ON f.doc_id = d.id
                     WHERE documents_fts MATCH ?
                     ORDER BY rank LIMIT 100"""
            args = (fts_q,)
        try:
            async with db.execute(sql, args) as cursor:
                rows = await cursor.fetchall()
                return [Document(**dict(row)) for row in rows]
        except Exception as e:
            print(f"FTS query error: {e}")
            return []


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


async def upsert_document(doc: Document) -> str:
    """Insert a document, or if one already exists for (url, search_query),
    refresh its content in place and keep its existing id. Returns the id that
    is now authoritative for that (url, search_query). Used by mission
    collection so the mission_documents join table never points at an orphaned
    id (unlike INSERT OR REPLACE, which mints a new id on conflict)."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM documents WHERE url = ? AND search_query = ?",
            (doc.url, doc.search_query),
        ) as cur:
            existing = await cur.fetchone()

        doc_id = existing["id"] if existing else doc.id
        if existing:
            await db.execute(
                """UPDATE documents SET
                       domain=?, title=?, crawled_at=?, content_markdown=?,
                       content_fit=?, word_count=?, links_internal=?,
                       links_external=?, metadata_json=?
                   WHERE id=?""",
                (doc.domain, doc.title, doc.crawled_at, doc.content_markdown,
                 doc.content_fit, doc.word_count, doc.links_internal,
                 doc.links_external, doc.metadata_json, doc_id),
            )
        else:
            await db.execute(
                """INSERT INTO documents
                   (id, url, domain, title, search_query, crawled_at,
                    content_markdown, content_fit, word_count,
                    links_internal, links_external, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, doc.url, doc.domain, doc.title, doc.search_query,
                 doc.crawled_at, doc.content_markdown, doc.content_fit,
                 doc.word_count, doc.links_internal, doc.links_external,
                 doc.metadata_json),
            )
        await db.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
        await db.execute(
            """INSERT INTO documents_fts(doc_id, title, domain, url, content)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, doc.title or "", doc.domain, doc.url, doc.content_markdown or ""),
        )
        await db.commit()
        return doc_id


# --- Agents ---

async def insert_agent(agent: Agent) -> bool:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO agents
               (id, name, expertise, persona_prompt, default_max_passes,
                default_max_sources, default_per_req_attempts, schedule_cron,
                active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent.id, agent.name, agent.expertise, agent.persona_prompt,
             agent.default_max_passes, agent.default_max_sources,
             agent.default_per_req_attempts, agent.schedule_cron,
             agent.active, agent.created_at),
        )
        await db.commit()
        return True


async def get_agent(agent_id: str) -> Optional[Agent]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)) as cur:
            row = await cur.fetchone()
            return Agent(**dict(row)) if row else None


async def delete_agent(agent_id: str) -> None:
    """Delete the agent row only. Its past missions, requirements, and the
    crawled documents are preserved (missions render gracefully without their
    agent)."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await db.commit()


async def list_agents(active_only: bool = False) -> list[Agent]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM agents"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY created_at DESC"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
            return [Agent(**dict(row)) for row in rows]


async def list_scheduled_agents() -> list[Agent]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM agents WHERE active = 1 AND schedule_cron IS NOT NULL AND schedule_cron != ''"
        ) as cur:
            rows = await cur.fetchall()
            return [Agent(**dict(row)) for row in rows]


# --- Missions ---

async def insert_mission(mission: Mission) -> bool:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO missions
               (id, agent_id, question, status, plan_json, budget_json,
                brief_markdown, job_id, parent_mission_id, error,
                created_at, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mission.id, mission.agent_id, mission.question, mission.status,
             mission.plan_json, mission.budget_json, mission.brief_markdown,
             mission.job_id, mission.parent_mission_id, mission.error,
             mission.created_at, mission.started_at, mission.finished_at),
        )
        await db.commit()
        return True


async def update_mission(mission_id: str, **fields) -> None:
    if not fields:
        return
    db_path = get_db_path()
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE missions SET {cols} WHERE id = ?",
            (*fields.values(), mission_id),
        )
        await db.commit()


async def get_mission(mission_id: str) -> Optional[Mission]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)) as cur:
            row = await cur.fetchone()
            return Mission(**dict(row)) if row else None


async def list_missions(limit: int = 50) -> list[Mission]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM missions ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [Mission(**dict(row)) for row in rows]


async def get_missions_enriched(limit: int = 50) -> list[dict]:
    """Missions with requirement coverage + document counts, for the History
    timeline and the Library collection selector."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT m.id, m.question, m.status, m.created_at, m.agent_id, m.job_id,
                      (SELECT COUNT(*) FROM requirements r WHERE r.mission_id = m.id) AS req_total,
                      (SELECT COUNT(*) FROM requirements r WHERE r.mission_id = m.id
                       AND r.status = 'satisfied') AS req_satisfied,
                      (SELECT COUNT(DISTINCT md.document_id) FROM mission_documents md
                       WHERE md.mission_id = m.id) AS doc_count
               FROM missions m ORDER BY m.created_at DESC LIMIT ?""",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_distinct_search_queries(limit: int = 100) -> list[dict]:
    """Distinct one-shot search queries (from the searches table, so this
    excludes agentic missions). Used by the Library collection selector."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT query, COUNT(*) AS runs, MAX(executed_at) AS last_run
               FROM searches GROUP BY query ORDER BY last_run DESC LIMIT ?""",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_latest_finished_mission(agent_id: str, question: str, before_mission_id: str) -> Optional[Mission]:
    """Most recent done mission for the same agent+question, excluding the given
    mission. Used for the Phase 2 delta ('what's new since last run')."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM missions
               WHERE agent_id = ? AND question = ? AND status = 'done' AND id != ?
               ORDER BY finished_at DESC LIMIT 1""",
            (agent_id, question, before_mission_id),
        ) as cur:
            row = await cur.fetchone()
            return Mission(**dict(row)) if row else None


# --- Requirements ---

async def insert_requirement(req: Requirement) -> bool:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO requirements
               (id, mission_id, title, description, rationale, status,
                attempts, next_queries_json, satisfied_doc_ids_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.id, req.mission_id, req.title, req.description, req.rationale,
             req.status, req.attempts, req.next_queries_json,
             req.satisfied_doc_ids_json),
        )
        await db.commit()
        return True


async def update_requirement(req_id: str, **fields) -> None:
    if not fields:
        return
    db_path = get_db_path()
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE requirements SET {cols} WHERE id = ?",
            (*fields.values(), req_id),
        )
        await db.commit()


async def get_requirements_for_mission(mission_id: str) -> list[Requirement]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM requirements WHERE mission_id = ? ORDER BY rowid", (mission_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [Requirement(**dict(row)) for row in rows]


# --- Mission ↔ document links ---

async def link_mission_document(mission_id: str, requirement_id: str, document_id: str) -> None:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR IGNORE INTO mission_documents
               (mission_id, requirement_id, document_id) VALUES (?, ?, ?)""",
            (mission_id, requirement_id, document_id),
        )
        await db.commit()


async def get_mission_documents(mission_id: str) -> list[Document]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT DISTINCT d.* FROM documents d
               JOIN mission_documents m ON m.document_id = d.id
               WHERE m.mission_id = ?
               ORDER BY d.crawled_at DESC""",
            (mission_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [Document(**dict(row)) for row in rows]


async def get_requirement_documents(mission_id: str, requirement_id: str) -> list[Document]:
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT d.* FROM documents d
               JOIN mission_documents m ON m.document_id = d.id
               WHERE m.mission_id = ? AND m.requirement_id = ?
               ORDER BY d.crawled_at DESC""",
            (mission_id, requirement_id),
        ) as cur:
            rows = await cur.fetchall()
            return [Document(**dict(row)) for row in rows]


async def get_prior_mission_urls(mission_id: str) -> set[str]:
    """All document URLs collected by any OTHER mission, for delta detection."""
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT DISTINCT d.url FROM documents d
               JOIN mission_documents m ON m.document_id = d.id
               WHERE m.mission_id != ?""",
            (mission_id,),
        ) as cur:
            rows = await cur.fetchall()
            return {row[0] for row in rows}
