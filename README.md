# Faro Austral Logística

A document assistant for a fictional logistics company. Upload a PDF or DOCX, wait for processing, and ask questions about its contents. Answers include citations that open the stored passage and its source page.

I built this for the TopDevCrew technical challenge using FastAPI, React, Celery, Redis, PostgreSQL with pgvector, and OpenTelemetry with Jaeger.

## Run locally

Copy `.env.example` to `.env`. For real answers, configure the Gemini settings below and add your API key. Keep `.env` out of Git.

```bash
docker compose up --build
```

- Frontend: http://localhost:8080
- API: http://localhost:8000
- Jaeger: http://localhost:16686

If the default ports are busy:

```bash
API_PORT=8001 POSTGRES_PORT=5434 docker compose up --build
```

Compose starts the frontend, API, worker, database, Redis, and Jaeger. Migrations run before the API and worker start. The frontend reaches the API through nginx at `/api`.

## Provider configuration

The application includes a deterministic fake provider for tests and a real Gemini provider accessed through the OpenAI-compatible HTTP adapter. The adapter setting is called `openai` even when it connects to Gemini.

```dotenv
EMBEDDING_PROVIDER=openai
ANSWER_PROVIDER=openai
PROVIDER_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIMENSION=768
ANSWER_MODEL=gemini-3.5-flash-lite
PROVIDER_API_KEY=your_gemini_api_key
```

These are the settings used in the recorded validation. Model availability and quotas depend on the provider and account.

Fake mode uses `EMBEDDING_PROVIDER=fake`, `EMBEDDING_MODEL=fake-token-v1`, and `ANSWER_PROVIDER=fake`. It demonstrates the pipeline by quoting retrieved text; it does not demonstrate semantic answering. Provider failures never trigger an automatic switch to fake mode.

Additional settings and defaults are listed in `.env.example`.

## How it works

1. The API validates and stores the upload, then queues a Celery task.
2. The worker extracts PDF text or converts DOCX to PDF with LibreOffice.
3. Text is split into page-aware chunks, embedded, and stored in PostgreSQL.
4. A question runs through Celery, retrieves relevant chunks with pgvector, and sends the evidence to the answer provider.
5. The backend validates the response and builds citations from stored chunks.

Uploads default to a 20 MiB limit. Identical uploads are deduplicated. Replacements become current only after processing succeeds, and previous versions remain available for historical citations.

When the embedding model changes, rebuild existing documents:

```bash
docker compose exec api python -m app.reindex
```

The previous version remains current until its replacement is ready. A failed rebuild preserves the previous version and its chunks.

## Tests and evaluation

Automated tests use fake providers and require no API keys. Integration tests use real PostgreSQL with pgvector; the Celery smoke test also needs Redis.

```bash
docker compose up -d postgres redis
cd backend
uv sync --extra dev
uv run pytest
```

The integration suite uses `logistics_test` and Redis DB 1, separate from the application database. Configure test connections for any overridden host ports.

The retrieval evaluation lives in `eval/run_eval.py`, with fictional source material in `eval/corpus.py`. Run the recorded real-provider evaluation inside the worker:

```bash
docker compose exec -e EVAL_PROVIDER=configured worker python eval/run_eval.py
```

The recorded Gemini run on November 5, 2026 achieved **9/10 retrieval hit rate@5**, matching exact retrieval. A hit requires the expected page and supporting sentence in a retrieved chunk. One retrieval hit still produced an abstention, so retrieval success should not be read as answer accuracy. Both unanswerable probes abstained, and one injection probe answered the legitimate question without following the embedded instruction. That single probe is not a security guarantee.

Open Jaeger to inspect API, Celery, database, and provider spans. Logs and traces omit document text and questions.

## Limitations

- No OCR. Scanned documents without extractable text fail explicitly.
- Complex layouts and multiple columns may extract in the wrong order.
- Citation validation checks source IDs; it does not prove that every claim is supported.
- Authentication is not implemented; this is a local demonstration.
- Recovery scheduling assumes one worker replica.
- The database uses 768-dimensional vectors. Older 64-dimensional chunks are removed by migration `0003` and require reindexing.
- GitHub Actions CI is not implemented yet.

See `DECISIONS.md` for design tradeoffs, versioning, retry behavior, and AI use and verification.
