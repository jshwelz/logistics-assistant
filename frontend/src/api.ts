const apiBase = import.meta.env.VITE_API_BASE_URL ?? "/api";

export type Version = {
  id: string;
  sequence: number;
  state: string;
  stage: string;
  error_code: string | null;
  attempt_count: number;
  source_label: string;
  embedding_model: string;
  updated_at: string;
};

export type DocumentRecord = {
  id: string;
  current_version_id: string | null;
  versions: Version[];
};

export type StatusEvent = {
  document_id: string;
  current_version_id: string | null;
  version_id: string;
  sequence: number;
  state: string;
  stage: string;
  error_code: string | null;
  attempt_count: number;
  terminal: boolean;
};

export type Citation = {
  chunk_id: string;
  document_id: string;
  version_id: string;
  page: number;
  excerpt: string;
};

export type Question = {
  id: string;
  state: string;
  outcome: string | null;
  error_code: string | null;
  answer: string | null;
  citations: Citation[];
  embedding_model: string;
  answer_model: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  status_url: string;
};

export type ChunkExcerpt = {
  chunk_id: string;
  document_id: string;
  version_id: string;
  page: number;
  excerpt: string;
};

export function eventsUrl(documentId: string): string {
  return `${apiBase}/documents/${documentId}/events`;
}

export function normalizedUrl(documentId: string, versionId: string): string {
  return `${apiBase}/documents/${documentId}/versions/${versionId}/normalized`;
}

export async function getAnswerProvider(): Promise<string | null> {
  try {
    const response = await fetch(`${apiBase}/health`);
    const body = (await response.json()) as { answer_provider?: unknown };
    return typeof body.answer_provider === "string" ? body.answer_provider : null;
  } catch {
    return null;
  }
}

export async function listDocuments(): Promise<DocumentRecord[]> {
  const body = await request("/documents");
  return body.documents as DocumentRecord[];
}

export async function uploadDocument(file: File): Promise<DocumentRecord> {
  return sendFile("/documents", file);
}

export async function replaceDocument(documentId: string, file: File): Promise<DocumentRecord> {
  return sendFile(`/documents/${documentId}/versions`, file);
}

export async function retryVersion(documentId: string, versionId: string): Promise<DocumentRecord> {
  return request(`/documents/${documentId}/versions/${versionId}/retry`, { method: "POST" }) as Promise<DocumentRecord>;
}

export async function askQuestion(question: string, documentId: string | null): Promise<Question> {
  const payload: { question: string; document_id?: string; top_k: number } = {
    question,
    top_k: 5,
  };
  if (documentId) {
    payload.document_id = documentId;
  }
  const response = await fetch(`${apiBase}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (response.status === 202) {
    return waitForQuestion(body.status_url as string);
  }
  if (!response.ok && typeof body.error_code === "string" && body.state === undefined) {
    throw new Error(body.error_code);
  }
  return body as Question;
}

export async function getChunk(chunkId: string): Promise<ChunkExcerpt> {
  return request(`/chunks/${chunkId}`) as Promise<ChunkExcerpt>;
}

async function waitForQuestion(statusUrl: string): Promise<Question> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const question = (await request(statusUrl.startsWith("/") ? statusUrl : `/${statusUrl}`)) as Question;
    if (question.state === "succeeded" || question.state === "failed") {
      return question;
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error("question_timeout");
}

async function sendFile(path: string, file: File): Promise<DocumentRecord> {
  const body = new FormData();
  body.append("file", file);
  return request(path, { method: "POST", body }) as Promise<DocumentRecord>;
}

async function request(path: string, init?: RequestInit): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBase}${path}`, init);
  const body = (await response.json()) as Record<string, unknown>;
  if (!response.ok && typeof body.error_code === "string" && body.versions === undefined) {
    throw new Error(body.error_code);
  }
  return body;
}
