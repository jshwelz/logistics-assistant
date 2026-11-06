# Question trace

`ask-trace.json` is the Jaeger export of trace `07f133dcd9c47a8996dd4d0d7b54c17b`, saved on 6 November 2026 from the running collector. Jaeger has no volume; this file is the copy that survives a restart.

The trace is one real Gemini question, `aee6d160-f064-4e1d-b032-b9ce41210e84`, outcome `answered`. It contains:

- `logistics-api`: `POST /ask` and `apply_async/app.tasks.answer_question`
- `logistics-worker`: `run/app.tasks.answer_question`, `question.execute`, `question.retrieve`, and `question.provider`
- PostgreSQL `SELECT`, `INSERT`, and `UPDATE` spans, including the pgvector distance query

`question.execute` records retrieval latency 649.973 ms, provider latency 2183.19 ms, model `gemini-3.5-flash-lite`, and token counts 529 prompt / 178 completion.

The export was searched for the question text, the cited excerpt, and credential patterns. Those values are not in the file. SQL uses bound parameters.
