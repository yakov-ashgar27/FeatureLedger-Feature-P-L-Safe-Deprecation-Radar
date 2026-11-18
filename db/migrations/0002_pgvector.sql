-- Tier 3 semantic classifier storage. Only embeddings are stored; the source text
-- (PR titles, ticket bodies) is discarded after embedding.
-- Guarded so the core schema still migrates on a Postgres without pgvector (e.g. CI);
-- the docker-compose image (pgvector/pgvector:pg16) always has it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        CREATE EXTENSION IF NOT EXISTS vector;

        CREATE TABLE feature_embeddings (
            feature_id  uuid PRIMARY KEY REFERENCES features(id) ON DELETE CASCADE,
            org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            model       text NOT NULL,
            embedding   vector(1536) NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE subject_embeddings (
            org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            subject_type  triage_subject NOT NULL,
            subject_id    uuid NOT NULL,
            model         text NOT NULL,
            embedding     vector(1536) NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, subject_type, subject_id)
        );

        CREATE INDEX feature_embeddings_hnsw ON feature_embeddings USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX subject_embeddings_hnsw ON subject_embeddings USING hnsw (embedding vector_cosine_ops);
    ELSE
        RAISE NOTICE 'pgvector not available: skipping embedding tables (Tier 3 classifier disabled)';
    END IF;
END $$;
