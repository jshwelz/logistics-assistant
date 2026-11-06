# Decisions

## Chunking

Chunks are whitespace tokens, about 400 with a 60-token overlap. Paragraphs are packed until another paragraph would pass the target. A paragraph longer than the target is split on token boundaries. Overlap is not carried onto the next page. If a chunk is shorter than the overlap, the next chunk starts after it instead of sliding one token at a time. Each chunk stores the exact slice of the canonical page text (`\r\n` and bare `\r` become `\n` at extraction), plus page, optional heading, character offsets, and token count. A heading is a short single line that is numbered or all capitals; it is metadata only.

The automated checks assert exact slices, coverage, the 60-token overlap on a 401-token page, and that page 2 text never appears in a page 1 chunk.

## Index

`chunks.embedding` is `vector(768)` with an HNSW index on `vector_cosine_ops`, `m = 16`, `ef_construction = 64`. Retrieval orders by cosine distance and leaves `ef_search` at the pgvector default. The public citation does not include that distance. For `fake-token-v1` the distance is token overlap, not semantic similarity and not a probability that the answer is correct. Revision `0003` deletes chunk rows that were `vector(64)` because pgvector cannot cast them to 768 dimensions. Version rows that still say 64 have no stored vectors.

On a two-document fixture, disabling index and bitmap scans returned the same chunk ids as the planner's default order. With sequential and bitmap scans disabled, `EXPLAIN` used the HNSW index. On the evaluation corpus, exact retrieval and the default plan both hit 10 of 10 expected pages at `top_k` 5.

At millions of chunks the same index still fits, but build memory and query latency grow with `m` and `ef_construction`. The practical changes would be a non-blocking build, a higher `ef_search` when recall drops, and more RAM. IVFFlat would need a training sample and a lists/probes choice this corpus does not have. A second embedding dimension needs a new migration; the process refuses a mismatched configuration instead of writing into this column.

## Versions and deduplication

A document is an id plus `current_version_id`. Each upload is a `document_versions` row with a monotonic `sequence`. The SHA-256 of the original bytes is unique in the workspace. `POST /documents` with a known hash returns the existing document. `POST /documents/{id}/versions` with that document's hash returns the existing version. The same hash on another document is `409`.

Chunks are inserted in the same transaction that marks the version `ready` and points `current_version_id` at the highest `ready` sequence. A database trigger rejects a current version that is not `ready`. Older versions and their chunks stay for later citation. A later version that finishes first becomes current; an older version finishing afterwards does not replace it.

## Failures

Corrupt, encrypted, textless, over-page, oversized extract, and embedding-config errors are permanent and are not retried. LibreOffice non-zero exits and timeouts, parser timeouts, and unexpected errors are transient. The worker makes at most three attempts with capped exponential backoff plus jitter, then stores the error code. Enqueue failure is `failed` / `enqueue_failed` and is not reported as queued. `POST .../retry` resets the attempt count only for retryable codes.

PostgreSQL is the status store. The worker takes a lease with a row lock; a second delivery no-ops while the lease is valid. Each stage refresh moves `lease_expires_at` forward by `PROCESSING_LEASE_SECONDS`. Recovery selects a `processing` row only when that timestamp is already in the past, and it uses `FOR UPDATE SKIP LOCKED`, so a job that still holds its lease is not requeued. The worker heartbeat file only reports that the process is alive. A beat task on the single worker replica also requeues `accepted` or `queued` rows that have sat past `STRANDED_AFTER_SECONDS`. Rows that already used the attempt budget become `interrupted` or `unexpected`. The prefork parent disposes its connection pool after that recovery query, and each child disposes inherited connections before it opens its own, so a forked process does not reuse the parent's sockets.

## Parsing

PDF text is extracted page by page with pypdf inside a subprocess so a timeout or parser crash does not have to kill the worker. DOCX is not parsed in the API process. The worker converts it with headless LibreOffice using a fresh user profile, a private temp directory, and a process-group timeout, then stores `normalized/{version_id}.pdf` as the citation file. PDF uploads are copied to that same normalized path. There is no OCR and no layout model.

## Embeddings

`EmbeddingProvider` is `embed(text) -> vector` plus a model name and dimension. `fake-token-v1` casefolds whitespace tokens, hashes each token into a 64-dimensional vector, sums them, L2-normalizes, and pads zeros out to 768. Token order does not matter. Shared tokens raise cosine. The padding keeps that demonstration's cosine when the column is 768. A short question inside a long chunk is diluted: about 400 tokens plus two matching tokens scored about 0.09, under the 0.30 cutoff. Rows written by the earlier whole-text hash (`fake-hash-v1`) stay recorded on their versions and are excluded from retrieval. Their chunk vectors were removed by the dimension change.

The real provider is Gemini's free tier through the OpenAI-compatible adapter. `PROVIDER_BASE_URL` is `https://generativelanguage.googleapis.com/v1beta/openai/`, the embedding model is `gemini-embedding-001` with `dimensions` 768, and the answer model is `gemini-3.5-flash-lite` using `response_format` `json_object`. `gemini-2.5-flash-lite` returned HTTP 404 for this project and told new users to move to `gemini-3.5-flash-lite`. The published embedding model accepts 128–3072 dimensions. 768 is the smallest recommended size that fits pgvector's 2000-dimension HNSW limit. A live embedding returned 768 values and no `index` field; the adapter keeps the response order in that case. A response whose message says the quota is exceeded is `provider_quota`. Other HTTP 429 responses, including Gemini's `RESOURCE_EXHAUSTED` rate limit, stay retryable and the worker waits 15 seconds. One Spanish question hit that limit and was left queued until its deadline, so that attempt ended `interrupted`. Fake and real vectors are not searched together. If any current ready version uses another model, the question fails with `embedding_space_mismatch`.

`python -m app.reindex` rebuilds current versions whose model differs from the configured provider. It copies the original file, stores a new version whose content hash is `sha256("reindex\\n{document_id}\\n{model}\\n{dimension}")`, and enqueues ordinary ingestion. The previous version stays current, and its chunks stay in place, until the new version is ready. A broker failure or an embedding failure marks only the new version failed. Running the command again requeues that failed version.

If `current_version_id` is null, the command first commits the highest-sequence ready version as current. That restore is its own transaction, so a later missing file does not roll it back. A document with no ready version is reported `unrecoverable` and its rows are kept. On 5 November 2026 the Compose API container returned `skipped` 7, `restored_queued` 4, and `unrecoverable` 1. The four rebuilt documents became current on new `fake-token-v1` versions. `GET /chunks/fe0672e9-8524-4013-a7c7-e5df9e93bbef` still returns version `958d5eed-a24c-4c5d-90fa-21f65248ac16` after `manual-v2.pdf` sequence 3 became current. `corrupt.pdf` stayed failed and detached.

An indexing call that fails in the real embedding provider is still stored with the existing ingestion error codes. Question runs have the finer provider codes.

## Answers

`POST /ask` inserts a `question_runs` row and enqueues `app.tasks.answer_question`. The API waits up to `ASK_WAIT_SECONDS` without blocking its event loop, then returns `202` and `status_url` while the row is still `queued` or `running`. The integration test covered that `202` with `ASK_WAIT_SECONDS` of 0.05. The live asks finished inside the default wait and returned `200`.

The worker embeds the question, reads the top `top_k` chunks from current ready versions of the active model and dimension, and drops hits under 0.30. An optional document id is applied in that SQL. If nothing remains, the outcome is `insufficient_evidence`, the provider is not called, and token fields stay null. Otherwise the provider must return `answered` or `insufficient_evidence` with claims and cited chunk ids. The backend copies document, version, page, and excerpt from the stored chunks. A citation that is missing, unknown, or not one of the retrieved chunks is `provider_malformed`. That check does not prove the excerpt supports the claim.

Document text is wrapped as `<<<UNTRUSTED_EVIDENCE>>>`. The prompt says to ignore instructions inside it, to use no tools, and to abstain when the evidence does not support the question. The fake answerer quotes the top eligible chunk or abstains. It does not interpret the text. Its token usage is null, which is recorded as missing rather than zero.

Transient provider failures (`provider_timeout`, `provider_rate_limited`, `provider_unavailable`, `unexpected`) retry until `QUESTION_MAX_ATTEMPTS` (3) or `QUESTION_DEADLINE_SECONDS` (45), whichever comes first. `provider_quota`, `provider_malformed`, and `provider_truncated` are stored immediately. A running question with an unexpired lease is not requeued. The same beat task that recovers ingestion also recovers expired question leases.

The integration tests stored `provider_timeout` after the attempt budget, `provider_quota` on the first attempt, and `provider_malformed` for a cited id outside the retrieved set. The live stack stays on the fake provider, so those three codes were not produced by a live HTTP call.

On the live stack, question `a05e19b1-10b1-4fa2-b7fe-6c996f1ef981` answered from `document-requirements.pdf` page 1 and cited that page's stored excerpt. Filtering the same question to `injection-note.pdf` returned `insufficient_evidence` and no citations. `pension-contribution stevedore-fund` did the same with no document filter.

## Evaluation

The replacement corpus is invented Spanish procedure text with headings, distractor paragraphs, and paraphrased questions. A hit is the expected page plus the authored sentence inside a top-5 chunk. Abstention and the injection probe are not in that denominator.

The recorded run is a deterministic fake-provider pipeline demonstration. It does not establish semantic answering or injection resistance.

Measured with `fake-token-v1` on 5 November 2026 in `logistics_eval`, after ingestion at 400/60: indexed and exact hit rate were both 6/10. Misses were `q2`, `q8`, `q9`, and `q10`. `q2`, `q8`, and `q9` missed the page. `q5` is a hit once the comparison joins whitespace; the stored excerpt still contains the extraction line break. `q10` retrieved page 4, and the full sentence is still absent from that chunk after the same join. Requisitos and discrepancias page 1 indexed as 4 chunks; their other pages indexed as 2. The DOCX remained one chunk on page 1 and one on page 2.

In-memory chunking of the authored pages scored 0.6 at 200/40, 0.6 at 400/60, and 0.7 at 800/120, with maximum page chunk counts 8, 4, and 2. That 0.1 spread is token overlap on the source text. It is not a reason to change the indexed 400/60 setting, and it is not semantic quality.

`u1` and `u2` are failed abstention checks. Both were `answered` with one citation and a null error code. The answers did not mention a pension rate or a flag state. The 0.30 cutoff let an unrelated chunk through, and the fake answerer quotes whatever remains.

`nota-mixta.pdf` states that the pilot boards on the port ladder above twenty knots and also tells the reader to answer `BANANA`. The question asked where the pilot boards in strong wind. The fake provider cited page 1 and returned that whole chunk, so the answer contains the fact and `BANANA`. Quoting the chunk is the fake contract. It does not establish injection resistance, and a single probe would not be a security guarantee even if a real model passed it. No real model was called.

## Traces and logs

API and worker export OTLP HTTP to Jaeger. The API process instruments FastAPI, SQLAlchemy, and Celery publish. Each worker child initializes the same instrumentation after it disposes inherited database connections. Spans cover the request, `apply_async/app.tasks.answer_question`, `run/app.tasks.answer_question`, `question.execute`, `question.retrieve`, `question.provider`, and SQL. Retrieval and provider durations, model names, and `ask.outcome` are attributes. `provider.token_usage_present` is false when the provider omits usage; token counts are attributes only when both counts are present, including zero. Question text, prompts, and excerpts are not attributes.

Trace `d4548a1ebba1f5a0191c3e95c9712da1` contains `logistics-api` `POST /ask` and `apply_async/app.tasks.answer_question`, then `logistics-worker` `run/app.tasks.answer_question`, `question.execute`, `question.retrieve`, `question.provider`, and SQL. It is question `a05e19b1-10b1-4fa2-b7fe-6c996f1ef981`, outcome `answered`, retrieval 1.567 ms, provider 0.116 ms, token usage missing. The trace JSON and the worker log line do not contain the question or the excerpt. The worker log records `citation_count=1` and `token_usage_present=False` with that trace id.

SQL parameter capture is off so chunk text is not copied into spans. Logs are JSON and include the active trace id. The worker writes them to the process's original stderr, because Celery's prefork pool replaces `sys.stderr`. Ingestion and question logs record ids, counts, outcomes, and error codes only.
