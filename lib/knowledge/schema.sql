-- Canonical schema for ~/.knowledge-index/index.db
-- Applied by: sqlite3 ~/.knowledge-index/index.db < ~/.local/lib/knowledge/schema.sql
-- Spec: <root>/dev-setup/01-knowledge-infra.md (deliverable 2.3)
-- Idempotent: every CREATE uses IF NOT EXISTS so re-application is safe.

-- WAL is persisted in the database file, so this only has to run once — but
-- `indexer.ensure_db()` sets it too (Python's sqlite3 module never runs this
-- file directly), so this line only matters to whoever applies this schema by
-- hand via the `sqlite3 ... < schema.sql` line above.
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS docs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('files', 'notion')),  -- 'notion' is legacy: no live writer remains (Notion sunset 2026-05-28, substrate ARCH-072). All current rows are 'files'.
    source_ref      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    project         TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL,
    topics          TEXT NOT NULL DEFAULT '[]',     -- JSON array
    audience        TEXT NOT NULL DEFAULT '[]',     -- JSON array
    applies_to      TEXT NOT NULL DEFAULT '[]',     -- JSON array of globs
    depends_on      TEXT NOT NULL DEFAULT '[]',     -- JSON array
    last_reviewed   TEXT,                            -- ISO date or NULL
    authors         TEXT NOT NULL DEFAULT '[]',     -- JSON array
    body            TEXT NOT NULL,
    body_hash       TEXT NOT NULL,
    notion_view     INTEGER NOT NULL DEFAULT 0,    -- LEGACY: was '1 if also pushed to Notion as view-only'. No live writer remains; column kept as audit trail of the 2026-05-18 migration. See substrate ARCH-072.
    updated_at      TEXT NOT NULL,
    -- JSON object of every frontmatter field the columns above do not name.
    -- One column rather than one per field BECAUSE THE FIELD SET IS OPEN: the
    -- three this was added for (`tools`, `model`, `trigger`) were dropped at
    -- index time for as long as the table existed, and a survey of the corpus on
    -- 2026-09-08 found `model_floor`, `visibility`, `supersedes`, `superseded_by`,
    -- `decided`, `sourcing`, `source_author` and `implemented_in` being dropped
    -- alongside them. Naming three columns would have fixed three fields and left
    -- the same hole for the ninth. Query it with json_extract().
    frontmatter_extra TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_docs_kind    ON docs(kind);
CREATE INDEX IF NOT EXISTS idx_docs_project ON docs(project);
CREATE INDEX IF NOT EXISTS idx_docs_status  ON docs(status);
CREATE INDEX IF NOT EXISTS idx_docs_source  ON docs(source);
-- "Which agents are pinned to which model" is the query the per-field columns
-- would have made cheap; this keeps it cheap without fixing the field set.
CREATE INDEX IF NOT EXISTS idx_docs_model
    ON docs(json_extract(frontmatter_extra, '$.model'));

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    id, title, body, topics,
    content='docs', content_rowid='rowid'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts (rowid, id, title, body, topics)
    VALUES (new.rowid, new.id, new.title, new.body, new.topics);
END;

CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts (docs_fts, rowid, id, title, body, topics)
    VALUES ('delete', old.rowid, old.id, old.title, old.body, old.topics);
END;

CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts (docs_fts, rowid, id, title, body, topics)
    VALUES ('delete', old.rowid, old.id, old.title, old.body, old.topics);
    INSERT INTO docs_fts (rowid, id, title, body, topics)
    VALUES (new.rowid, new.id, new.title, new.body, new.topics);
END;

-- Reserved for future vector search
CREATE TABLE IF NOT EXISTS embeddings (
    id              TEXT PRIMARY KEY REFERENCES docs(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    vector          BLOB NOT NULL,
    updated_at      TEXT NOT NULL
);
