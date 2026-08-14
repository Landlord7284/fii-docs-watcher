-- Manifest schema.
--
-- Three tables, because three responsibilities are genuinely distinct and
-- collapsing them loses information:
--
--   documents          logical identity, current state, physical traceability
--   download_attempts  append-only history of every try, successful or not
--   sync_state         per-entity watermark and last error
--
-- Putting the attempt result on the document row would overwrite the previous
-- attempt each time, and a uniqueness constraint that included the state would
-- make it impossible to record two failures for one document -- which is
-- exactly what a retry flow needs to do.

CREATE TABLE IF NOT EXISTS documents (
    -- Publication identity. The dedupe key everywhere: a re-filing that keeps
    -- the same id but bumps the version is a different publication, and without
    -- the version in the key v2 would overwrite v1.
    document_id           INTEGER NOT NULL,
    version               INTEGER NOT NULL,

    -- The emitting entity, not the monitored scope. The scope-to-entities
    -- relation lives in funds.yaml; the manifest records the observed fact.
    fundosnet_id          INTEGER NOT NULL,
    entity_cnpj           TEXT,

    fund_description      TEXT,
    category              TEXT,
    doc_type              TEXT,
    species               TEXT,

    reference_date        TEXT,
    -- The source sends this discriminator as a string ('2', '3', '4'), so it is
    -- stored as one: 2 = month competence, 3 = date, 4 = date with time.
    reference_date_format TEXT,

    -- yyyy-mm-dd, from dataEntrega. The archiving axis and the purge key.
    delivery_date         TEXT NOT NULL,
    delivery_at           TEXT NOT NULL,

    modality              TEXT,
    -- Mutable at the source. Means: the last state observed INSIDE the
    -- retention window. A document that has fallen out of the window keeps its
    -- last known value, with no promise that it is still current.
    status                TEXT,

    -- discovered | downloading | available | failed | purged | skipped | abandoned
    --
    -- `skipped`   the configured [download].formats exclude this document's
    --             format. Not a failure, not retried as one, and re-evaluated
    --             each run so widening the configuration later picks it up.
    -- `abandoned` the fund was removed from the watch list before this document
    --             was fetched. Never retried; kept as a record of what the
    --             source published while the fund was still being followed.
    local_state           TEXT NOT NULL,

    path                  TEXT,
    extension             TEXT,
    -- Integrity and audit only. NEVER the dedupe key: two distinct publications
    -- may legitimately share bytes.
    content_hash          TEXT,

    downloaded_at         TEXT,
    purged_at             TEXT,
    -- Set when a higher version of the same document_id is observed. The file
    -- stays on disk: a superseded version is history worth keeping, and the
    -- listing stops returning it once the re-filing lands.
    superseded_at         TEXT,
    seen_at               TEXT NOT NULL,

    PRIMARY KEY (document_id, version)
);

CREATE INDEX IF NOT EXISTS idx_documents_delivery ON documents (delivery_date);
CREATE INDEX IF NOT EXISTS idx_documents_state    ON documents (local_state);
CREATE INDEX IF NOT EXISTS idx_documents_entity   ON documents (fundosnet_id, delivery_date);

CREATE TABLE IF NOT EXISTS download_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL,
    version      INTEGER NOT NULL,
    attempted_at TEXT NOT NULL,
    -- success | transient | invalid_content | error
    outcome      TEXT NOT NULL,
    http_status  INTEGER,
    bytes        INTEGER,
    duration_ms  INTEGER,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_document
    ON download_attempts (document_id, version, attempted_at);

CREATE TABLE IF NOT EXISTS sync_state (
    fundosnet_id     INTEGER PRIMARY KEY,
    -- Advanced only after a scan that proved complete coverage. An interrupted
    -- or short scan advances nothing.
    last_success_at  TEXT,
    last_window_end  TEXT,
    last_error       TEXT,
    last_error_at    TEXT
);
