-- RAG vector store moves from ChromaDB to TurboQuant (app/vectorstore.py).
-- The compressed vectors live in one index file per knowledge base ({VECTOR_DIR}/{kb_name}.tvim);
-- the text and metadata of every chunk live here. rag_chunks.id is the id of the chunk's vector in the index.
-- No foreign key to kb_catalog: the store also holds temporary indexes (doctor checks), and deleting a
-- knowledge base removes its chunks and file together (rag.drop_index).
CREATE TABLE IF NOT EXISTS rag_chunks (
    id          bigserial PRIMARY KEY,
    kb_name     text NOT NULL,
    source      text NOT NULL,          -- document file name
    page        integer,                -- PDF page, NULL for other formats
    chunk       integer NOT NULL,       -- position of the chunk in the document
    run_id      bigint,                 -- pipeline_runs.id of the upload that added it
    text        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_chunks_kb_source_idx ON rag_chunks (kb_name, source);

UPDATE kb_catalog SET storage_ref = 'turboquant:' || kb_name WHERE kb_type = 'rag' AND storage_ref LIKE 'chroma:%';
