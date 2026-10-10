-- Versioned transformation files. The ETL reads the active version on every
-- run; a repair is a new version of an allowlisted file, never an in-place
-- edit. Readable by the investigation tools.

CREATE SCHEMA IF NOT EXISTS pipeline;

CREATE TABLE IF NOT EXISTS pipeline.transform_versions (
    version      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_file  TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    author       TEXT NOT NULL,
    message      TEXT NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS transform_versions_one_active_idx
    ON pipeline.transform_versions (target_file) WHERE is_active;
