-- Runs once on a fresh Postgres volume (docker-entrypoint-initdb.d).
-- Alembic 0001 also creates `vector`; having it here means the extensions
-- exist before the first `migrate`.
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector embeddings
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- trigram title search
CREATE EXTENSION IF NOT EXISTS btree_gin;  -- composite indexes on ARRAY columns
