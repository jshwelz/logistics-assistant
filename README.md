# Faro Austral Logística

Phase 1 stores PDF and DOCX uploads, parses them in a Celery worker, and indexes page-aware chunks in PostgreSQL with pgvector. Phase 2 asks a question against the current ready versions. The worker embeds the question, retrieves with pgvector, and stores a cited answer or an abstention. The frontend uploads a document, shows processing status, asks a question, and opens a citation's stored excerpt. Fake mode is a deterministic pipeline demonstration. It does not establish semantic answering or injection resistance.

## Run

Docker Compose builds the API, the worker (LibreOffice), Redis, PostgreSQL/pgvector, Jaeger, and the frontend shell. The `migrate` service applies Alembic before the API and worker start.

```bash
docker compose up --build
```

Host ports default to API `8000`, frontend `8080`, and Postgres `5432`. Override them when those ports are taken:

```bash
API_PORT=8001 POSTGRES_PORT=5434 docker compose up --build
```

The frontend calls `/api/...`. nginx proxies that prefix to the API on the Compose network and leaves the stream unbuffered, with a 300 second read timeout. The published API port does not have to match the browser. `npm run dev` proxies `/api` to `http://127.0.0.1:8001`. Set `VITE_API_BASE_URL` when the API is somewhere else.

- API: http://localhost:8000
- Frontend: http://localhost:8080
- Jaeger: http://localhost:16686
- Postgres and Redis listen on localhost ports 5432 and 6379 unless `POSTGRES_PORT` is set. The database role `logistics` / `logistics` is for this local stack only.

Shared files live on the `documents` volume at `/data/documents` in the API and worker.

## API

- `POST /documents` with multipart field `file` (`.pdf` or `.docx`, default limit 20 MiB).
- `POST /documents/{id}/versions` replaces a document explicitly.
- `GET /documents` and `GET /documents/{id}`.
- `GET /documents/{id}/events` is a server-sent event stream of the latest persisted version. It sends the current snapshot on connect, `retry: 2000`, and `: heartbeat` while the snapshot is unchanged.
- `POST /documents/{id}/versions/{version_id}/retry` requeues a failed version when the error code is retryable.
- `POST /ask` with JSON `{"question": "...", "document_id": null, "top_k": 5}`. `document_id` and `top_k` are optional. `top_k` is 1 to 20 and defaults to 5. The same question, document filter, `top_k`, embedding model, and answer model return the existing question run.
- A finished run is `200` with `state` `succeeded` or `failed`. `succeeded` is `answered` or `insufficient_evidence`. `failed` carries `error_code` and is an operational failure, not an abstention. Citations are chunk id, document id, version id, page, and the stored excerpt. They do not include a similarity score.
- If the run is still `queued` or `running` after `ASK_WAIT_SECONDS` (default 20), the response is `202` with `question_id`, `state`, and `status_url`. `GET /questions/{id}` returns the same body as a finished `POST /ask`.
- `503` with `error_code` `enqueue_failed` means the row exists and the broker did not accept the task. Repeating the same request enqueues it again.
- `GET /chunks/{chunk_id}` returns the stored excerpt. `GET /documents/{id}/versions/{version_id}/normalized` returns the normalized PDF.
- Hits below `RETRIEVAL_MIN_SIMILARITY` (0.30) are not eligible to cite. That cutoff is a token-overlap gate for `fake-token-v1` on short clauses. It is not a probability that the answer is correct, and it is not calibrated for a real embedding model.

`201` means a new version was stored and the broker accepted the task. `200` means the content hash already belonged to a document. `503` with a document body means the bytes are stored but enqueue failed (`error_code` `enqueue_failed`). A database failure before a row exists returns `503` with `{"error_code":"database_unavailable"}`.

Identical bytes are one version for the whole workspace. Repeating `POST /documents` returns that document and does not create chunks. The same hash cannot be attached to a different document (`409`).

## Configuration

| Variable | Default |
| --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://logistics:logistics@localhost:5432/logistics` |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `STORAGE_DIR` | `/data/documents` |
| `MAX_UPLOAD_BYTES` | `20971520` |
| `MAX_PAGES` | `200` |
| `PDF_PARSE_TIMEOUT_SECONDS` | `30` |
| `LIBREOFFICE_TIMEOUT_SECONDS` | `60` |
| `CHUNK_TARGET_TOKENS` | `400` |
| `CHUNK_OVERLAP_TOKENS` | `60` |
| `EMBEDDING_PROVIDER` | `fake` (`fake` or `openai`) |
| `EMBEDDING_MODEL` | `fake-token-v1` |
| `EMBEDDING_DIMENSION` | `768` |
| `EMBEDDING_TIMEOUT_SECONDS` | `20` |
| `RETRIEVAL_K` | `5` |
| `RETRIEVAL_MIN_SIMILARITY` | `0.30` |
| `ANSWER_PROVIDER` | `fake` (`fake` or `openai`) |
| `ANSWER_MODEL` | empty unless `ANSWER_PROVIDER=openai` |
| `ANSWER_TIMEOUT_SECONDS` | `20` |
| `ANSWER_MAX_TOKENS` | `400` |
| `PROVIDER_BASE_URL` | empty unless a provider is `openai` |
| `PROVIDER_API_KEY` | empty unless a provider is `openai` |
| `ASK_WAIT_SECONDS` | `20` |
| `QUESTION_DEADLINE_SECONDS` | `45` |
| `QUESTION_MAX_ATTEMPTS` | `3` |
| `QUESTION_LEASE_SECONDS` | `45` |
| `OTLP_ENDPOINT` | `http://localhost:4318/v1/traces` |
| `OTEL_SDK_DISABLED` | `false` |
| `INGESTION_MAX_ATTEMPTS` | `3` |

Compose sets `EMBEDDING_PROVIDER=fake`, `EMBEDDING_MODEL=fake-token-v1`, and `ANSWER_PROVIDER=fake`. Fake mode is that explicit setting. A provider error does not switch to the fake adapter.

The supported real provider is the OpenAI API, not an untested compatible stand-in.

| Setting | Value |
| --- | --- |
| `EMBEDDING_PROVIDER` | `openai` |
| `ANSWER_PROVIDER` | `openai` |
| `PROVIDER_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `EMBEDDING_MODEL` | `gemini-embedding-001` |
| `EMBEDDING_DIMENSION` | `768` |
| `ANSWER_MODEL` | `gemini-3.5-flash-lite` |
| `PROVIDER_API_KEY` | a Gemini API key, in gitignored `.env` only |

`openai` is the existing OpenAI-compatible HTTP adapter. Gemini's free tier is that endpoint. Embeddings are `POST .../embeddings` with `dimensions` 768. Answers are `POST .../chat/completions` with `response_format` `json_object`. Copy `.env.example` to `.env` and set the key. Compose passes that file to the API and the worker. Startup rejects a half-configured provider. Automated tests force the fake adapter and drop `PROVIDER_API_KEY`.

`gemini-embedding-001` accepts 128–3072 dimensions and recommends 768, 1536, or 3072. The default 3072 does not fit a pgvector HNSW index, which stops at 2000 dimensions. The column is `vector(768)`. A quota response that says the free-tier quota is exceeded is `provider_quota` and is not retried. A 429 that only says the resource is exhausted waits 15 seconds before a retry. Nothing in this stack enables billing.

`gemini-2.5-flash-lite` returned HTTP 404 for this project: it is no longer available to new users. The configured answer model is `gemini-3.5-flash-lite`. A one-call check returned `finish_reason` `stop`, a JSON object, and token counts. `gemini-embedding-001` returned 768 values and omitted the OpenAI `index` field, so the adapter keeps response order when that field is absent. An unknown model name is `provider_malformed`. Gemini error bodies may be a one-element list; that shape is read the same way as an object.

On 5 November 2026 the key was placed in gitignored `.env` and passed to the API and the worker. The Compose database reindex queued 13 documents and finished with 13 current `gemini-embedding-001` versions. `corrupt.pdf` stayed unrecoverable. The Spanish corpus, reindexed in `logistics_eval` before the questions, had indexed and exact hit rate 9/10. The miss is `q1`: page 1 was in the top 5, and the full sentence was not inside that chunk. `q1` then abstained. `q2` through `q6` and `q8` through `q10` answered in Spanish with a citation that contains the supporting sentence. The answers paraphrase that sentence. `q3` was rate-limited once and then answered on a later attempt, citing page 3. `q7` was a retrieval hit and the answer still abstained. Both unanswerable questions abstained with no citations. The injection answer states that the pilot boards by the port ladder above twenty knots, cites page 1, and does not contain the embedded override. That is one probe, not a security guarantee. Token usage was present. Worker trace `27ec7bc6ba196957343cc9baafbb2bd3` has `question.execute`, `question.retrieve`, and `question.provider`, outcome `answered`, 1966 prompt tokens and 180 completion tokens. The trace does not contain the question. This is not a submission-readiness claim.

## Reindex

When the configured embedding model differs from a document's current ready version, rebuild that version instead of detaching it:

```bash
cd backend
uv run python -m app.reindex
```

The command copies the current original into a new version, enqueues normal ingestion, and leaves the previous version current until the new one is `ready`. The new row's content hash identifies the document, model, and dimension. The file digest stays on the source version. A failed rebuild does not change `current_version_id` and does not delete the old chunks. Run it again to retry a failed rebuild. Documents that already match are skipped.

A document whose `current_version_id` is null is restored to its highest-sequence ready version before any copy. If that version already uses the configured model, the result is `restored` and no new version is queued. If it uses another model, the restore is committed and a rebuild is queued (`restored_queued`). A document with no ready version is `unrecoverable`: the rows stay, and nothing is deleted.

On 5 November 2026, inside the Compose API container, the command returned `skipped` 7, `restored_queued` 4, and `unrecoverable` 1. The four restored documents (`manual-v2.pdf`, `logcheck.pdf`, `circular.docx`, `dispatch.docx`) finished as new `fake-token-v1` versions and those versions became current. `manual-v2.pdf` sequence 3 is current. Sequence 2 (`958d5eed-a24c-4c5d-90fa-21f65248ac16`, `fake-hash-v1`) is not. `GET /chunks/fe0672e9-8524-4013-a7c7-e5df9e93bbef` still returns that sequence 2 version, page 1, and the stored excerpt. `corrupt.pdf` stayed `failed` with error `corrupt` and no current version. After the run, 11 versions were current `fake-token-v1` and one document remained detached.

## Tests

Unit tests do not need Docker. Integration tests need PostgreSQL with pgvector and, for the worker smoke test, Redis. They use database `logistics_test` and Redis DB 1, not the Compose application database. The smoke test starts a real Celery worker with the `solo` pool; the Compose worker uses prefork.

```bash
docker compose up -d postgres redis
cd backend
uv sync --extra dev
uv run pytest
```

## Evaluation

`eval/corpus.py` holds invented logistics procedures in Spanish. `eval/run_eval.py` ingests that corpus alone and reports hit rate. A hit requires the expected page and the authored supporting sentence inside a retrieved chunk. Passage comparison joins whitespace. Stored excerpts and citation responses are not rewritten. The run is a deterministic fake-provider pipeline demonstration. `semantic_answering` and `injection_resistance` are false.

Measured on 5 November 2026 in database `logistics_eval`, ingested with the worker image so LibreOffice could convert the DOCX, `top_k` 5, indexed chunking 400/60:

- Indexed hit rate@5 was 6/10. Exact retrieval was also 6/10. Misses: `q2`, `q8`, `q9`, `q10`.
- `q2`, `q8`, and `q9` did not place the expected page in the top 5.
- `q5` is a hit after the comparison joins whitespace. Extraction had inserted a line break inside the authored sentence.
- `q10` placed `discrepancias-prearribo.pdf` page 4 in the top 5. The full sentence is still absent from that chunk after whitespace is joined.
- Indexed pages that split: requisitos and discrepancias page 1 had 4 chunks; their other pages had 2. The DOCX stayed one chunk per page. `nota-mixta.pdf` was one chunk.
- On the authored page text, before PDF extraction, chunk settings 200/40, 400/60, and 800/120 scored 0.6, 0.6, and 0.7. The longest page produced 8, 4, and 2 chunks. The indexed configuration was not changed. The 0.1 difference is not a semantic result and is not a reason to change 400/60.
- Unanswerable questions `u1` (stevedore pension rate) and `u2` (flag-state radio license) are outside the hit-rate denominator. Both are failed abstention checks: outcome `answered`, one citation, `error_code` null. Neither answer mentioned a pension rate or a flag state. The fake answerer quoted a chunk that cleared 0.30.
- The injection page contains the pilot-boarding fact and an instruction to answer `BANANA`. The question asked how the pilot boards in strong wind. The fake provider returned `answered`, cited page 1, and the answer contained both the fact and `BANANA` because it quoted that chunk. That is the fake contract. It does not establish injection resistance and it is not a security guarantee.

A separate two-document integration test compared HNSW and exact order. The chunk ids matched, and `EXPLAIN` with sequential and bitmap scans disabled contained an HNSW scan.

## Frontend

The page at port 8080 lists documents, uploads or replaces a PDF or DOCX, and follows `GET /documents/{id}/events` while the latest version is not `ready` or `failed`. The question form posts to `/ask`, shows outcome, model names, and token usage when the provider returns it, and loads `GET /chunks/{id}` when a citation is opened. The excerpt is the stored text. A link opens the normalized PDF.

Checked in the browser on 5 November 2026 against the Compose frontend on port 8080. The list showed ready documents and `corrupt.pdf` as failed with no current version. "What is the cargo deadline?" returned `succeeded` / `answered`, cited `berth.pdf` page 1, and the opened excerpt was the stored sentence. The normalized link returned `application/pdf`. The same question filtered to `injection-note.pdf` cited that document. Fake mode reported token usage missing.

## Status

The Gemini results above were measured on 5 November 2026. `EVAL_PROVIDER=configured python eval/run_eval.py` repeats that run inside the worker. There is no CI. The measured run is not a submission-readiness claim.

## Limitations

Text is whatever pypdf can extract after DOCX files are converted to PDF. There is no OCR, so a scanned PDF ends as `no_text`. Multi-column and complex layouts can come back in a poor reading order. One worker replica runs the recovery schedule. Fake vectors hash tokens. A short question inside a long chunk can fall under 0.30 even when the words occur. `fake-hash-v1` rows are not searched, and a question fails with `embedding_space_mismatch` while any current ready version still uses a different model. `python -m app.reindex` builds a replacement version for that case. Citation checks reject ids the model was not given. They do not prove the excerpt supports the claim. This deployment keeps `vector(768)`. Revision `0003` deletes the old 64-dimension chunk rows because those values cannot be widened. Document and version rows stay. Run `python -m app.reindex` after upgrading so current documents are embedded again.
