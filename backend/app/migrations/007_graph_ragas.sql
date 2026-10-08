-- Existing evaluation rows were created for RAG KBs. New runs persist their actual KB type.
ALTER TABLE rag_eval_runs ADD COLUMN IF NOT EXISTS kb_type text NOT NULL DEFAULT 'rag';
