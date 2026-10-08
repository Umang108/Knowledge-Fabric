-- 1. Knowledge base names may use upper-case letters. Names stay unique without regard to case
--    ("Sales_KG" and "sales_kg" would map to the same Neo4j database in multi mode).
ALTER TABLE kb_catalog DROP CONSTRAINT IF EXISTS kb_catalog_kb_name_check;
ALTER TABLE kb_catalog ADD CONSTRAINT kb_catalog_kb_name_check CHECK (kb_name ~ '^[A-Za-z][A-Za-z0-9_]{2,62}$');
CREATE UNIQUE INDEX IF NOT EXISTS kb_catalog_kb_name_ci_uq ON kb_catalog (lower(kb_name));

-- 2. PII classified with NIST SP 800-122: identifier type, confidentiality impact level and the factors behind it.
ALTER TABLE kb_pii_fields
    ADD COLUMN IF NOT EXISTS nist_identifier text CHECK (nist_identifier IN ('direct', 'linkable')),
    ADD COLUMN IF NOT EXISTS nist_impact     text CHECK (nist_impact IN ('low', 'moderate', 'high')),
    ADD COLUMN IF NOT EXISTS nist_factors    jsonb;

-- 3. Chat history: the last 10 conversations per user (older ones are deleted when a new one starts).
CREATE TABLE IF NOT EXISTS chat_conversations (
    id           bigserial PRIMARY KEY,
    user_id      text NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    kb_name      text NOT NULL REFERENCES kb_catalog (kb_name) ON DELETE CASCADE ON UPDATE CASCADE,
    title        text NOT NULL,
    last_message_at timestamptz NOT NULL DEFAULT now(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    modified_by  text NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_conversations_user_idx ON chat_conversations (user_id, last_message_at DESC);
CREATE TRIGGER chat_conversations_set_updated_at BEFORE UPDATE ON chat_conversations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS chat_messages (
    id               bigserial PRIMARY KEY,
    conversation_id  bigint NOT NULL REFERENCES chat_conversations (id) ON DELETE CASCADE,
    question         text NOT NULL,
    answer           text NOT NULL,
    details          jsonb NOT NULL DEFAULT '{}',   -- kind, cypher, path, row_count, rows, sources
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    modified_by      text NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_conversation_idx ON chat_messages (conversation_id, id);
CREATE TRIGGER chat_messages_set_updated_at BEFORE UPDATE ON chat_messages
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
