-- rework-agent-1-v5 / tasks.md 4.2
-- Build the HNSW index AFTER the bulk embedding load, not before (design D4):
-- building it on an empty/partial column and then inserting is much slower than
-- one build over the finished set. Only on clean_posts.embedding; cosine ops.
-- pgvector 0.6.0 (installed) supports hnsw.
--
-- NOTE: run this ONLY after embed_v5 has filled the embeddings (task 4.2). The
-- build scans every embedded row and can take a while on a large corpus -- that
-- is expected. Not wrapped in an explicit transaction.

CREATE INDEX IF NOT EXISTS clean_posts_embedding_hnsw
ON agent_1_v5.clean_posts
USING hnsw (embedding vector_cosine_ops);