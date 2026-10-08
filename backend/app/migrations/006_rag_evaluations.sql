-- RAGAS evaluations of RAG and graph knowledge bases (python -m app.cli eval-ragas, app/eval_rag.py).
-- One row per run with the average of each metric, and one row per question with its answer, the passages the
-- model was given and every metric's score, so runs can be compared over time.
CREATE TABLE IF NOT EXISTS rag_eval_runs (
    id            bigserial PRIMARY KEY,
    kb_name       text NOT NULL,          -- no foreign key: results stay after the knowledge base is deleted
    kb_type       text NOT NULL DEFAULT 'rag', -- rag or graph
    testset       text NOT NULL,          -- test set file name
    questions     integer NOT NULL,
    metrics       jsonb NOT NULL,         -- {"faithfulness": 0.91, ...} averages over the questions scored
    config        jsonb NOT NULL,         -- models, judge, top-k, TurboQuant bits, guardrail settings
    report_dir    text,                   -- folder with results.csv / results.jsonl / summary.json
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL
);
CREATE INDEX IF NOT EXISTS rag_eval_runs_kb_idx ON rag_eval_runs (kb_name, started_at DESC);

CREATE TABLE IF NOT EXISTS rag_eval_results (
    id            bigserial PRIMARY KEY,
    run_id        bigint NOT NULL REFERENCES rag_eval_runs (id) ON DELETE CASCADE,
    question_id   text NOT NULL,
    question      text NOT NULL,
    reference     text,                   -- expected answer from the test set (optional)
    answer        text NOT NULL,          -- what the app answered (after guardrails)
    contexts      jsonb NOT NULL,         -- passages given to the model (masked as in the app)
    sources       jsonb NOT NULL,         -- [{source, page, score}]
    scores        jsonb NOT NULL,         -- {"faithfulness": 1.0, "context_recall": null, ...}
    errors        jsonb NOT NULL DEFAULT '{}',
    latency_ms    integer,
    trace_id      text                    -- Langfuse trace of the answer, when tracing is on
);
CREATE INDEX IF NOT EXISTS rag_eval_results_run_idx ON rag_eval_results (run_id);
