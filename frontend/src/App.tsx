import {
  FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import {
  type ChunkExcerpt,
  type Citation,
  type DocumentRecord,
  type Question,
  type StatusEvent,
  askQuestion,
  eventsUrl,
  getAnswerProvider,
  getChunk,
  listDocuments,
  normalizedUrl,
  replaceDocument,
  retryVersion,
  uploadDocument,
} from "./api";

const RETRYABLE = new Set([
  "conversion_failed",
  "conversion_timeout",
  "parse_timeout",
  "enqueue_failed",
  "interrupted",
  "unexpected",
]);

const SUGGESTIONS = ["What obligations are described?", "Which deadlines are mentioned?"];

const STAGE_LABEL: Record<string, string> = {
  persisted: "Saved",
  queued: "Queued",
  parsing: "Reading the file",
  chunking: "Splitting into passages",
  embedding: "Indexing",
  indexed: "Ready",
};

const DOCUMENT_ERROR: Record<string, string> = {
  corrupt: "This file could not be read",
  encrypted: "This file is encrypted",
  no_text: "No text was found",
  too_many_pages: "This file has too many pages",
  conversion_failed: "The file could not be converted",
  conversion_timeout: "Conversion timed out",
  parse_timeout: "Reading the file timed out",
  extract_too_large: "The extracted text is too large",
  enqueue_failed: "Processing could not be started",
  interrupted: "Processing was interrupted",
  embedding_config_mismatch: "The index settings do not match",
  unexpected: "Processing failed",
};

const QUESTION_ERROR: Record<string, string> = {
  provider_timeout: "The answer service timed out.",
  provider_rate_limited: "The answer service is busy. Try again shortly.",
  provider_quota: "The answer service quota was reached.",
  provider_malformed: "The answer service returned an unusable response.",
  provider_truncated: "The answer was cut off.",
  provider_unavailable: "The answer service is unavailable.",
  embedding_space_mismatch: "These documents were indexed with a different model.",
  enqueue_failed: "The question could not be started.",
  interrupted: "The question was interrupted.",
  unexpected: "The question failed.",
  question_timeout: "The question is still running.",
  upload_failed: "The file could not be uploaded.",
  replace_failed: "The file could not be replaced.",
  retry_failed: "Processing could not be restarted.",
  excerpt_failed: "The stored excerpt could not be opened.",
  unavailable: "The document list could not be loaded.",
};

type Turn = {
  id: string;
  text: string;
  scopeLabel: string;
  pending: boolean;
  question: Question | null;
  error: string | null;
};

type DrawerState = {
  citation: Citation;
  excerpt: ChunkExcerpt | null;
  error: string | null;
};

export function App() {
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [scopeId, setScopeId] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const [provider, setProvider] = useState<string | null>(null);
  const [tab, setTab] = useState<"documents" | "chat">("documents");
  const citationFocus = useRef<HTMLElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      setDocuments(await listDocuments());
      setListError(null);
    } catch (error) {
      setListError(error instanceof Error ? error.message : "unavailable");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    void getAnswerProvider().then(setProvider);
  }, []);

  const applyStatus = useCallback((event: StatusEvent) => {
    setDocuments((current) =>
      current.map((document) => {
        if (document.id !== event.document_id) {
          return document;
        }
        return {
          ...document,
          current_version_id: event.current_version_id,
          versions: document.versions.map((version) =>
            version.id === event.version_id
              ? {
                  ...version,
                  state: event.state,
                  stage: event.stage,
                  error_code: event.error_code,
                  attempt_count: event.attempt_count,
                }
              : version,
          ),
        };
      }),
    );
  }, []);

  const readyCount = documents.filter((document) => document.current_version_id !== null).length;

  function selectDocument(documentId: string) {
    setScopeId(documentId);
  }

  async function openCitation(citation: Citation, source: HTMLElement) {
    citationFocus.current = source;
    setDrawer({ citation, excerpt: null, error: null });
    try {
      const excerpt = await getChunk(citation.chunk_id);
      setDrawer((current) =>
        current?.citation.chunk_id === citation.chunk_id ? { ...current, excerpt } : current,
      );
    } catch (caught) {
      const code = caught instanceof Error ? caught.message : "excerpt_failed";
      setDrawer((current) =>
        current?.citation.chunk_id === citation.chunk_id ? { ...current, error: code } : current,
      );
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <p className="brand-name">Faro Austral</p>
          <p className="brand-sub">Document Assistant</p>
        </div>
        {provider === "fake" ? (
          <div className="demo">
            <span className="badge" aria-describedby="demo-note">
              Demo mode
            </span>
            <p id="demo-note">Uses a simulated provider. Answers may quote text without understanding it.</p>
          </div>
        ) : null}
      </header>
      <div className="tabs" role="tablist" aria-label="Workspace">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "documents"}
          className={tab === "documents" ? "tab active" : "tab"}
          onClick={() => setTab("documents")}
        >
          Documents
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "chat"}
          className={tab === "chat" ? "tab active" : "tab"}
          onClick={() => setTab("chat")}
        >
          Chat
        </button>
      </div>
      <div className="workspace">
        <Documents
          active={tab === "documents"}
          documents={documents}
          error={listError}
          scopeId={scopeId}
          onSelect={selectDocument}
          onChange={refresh}
          onStatus={applyStatus}
        />
        <Chat
          active={tab === "chat"}
          documents={documents}
          readyCount={readyCount}
          scopeId={scopeId}
          turns={turns}
          onScope={setScopeId}
          onTurns={setTurns}
          onOpenCitation={openCitation}
        />
      </div>
      {drawer ? (
        <CitationDrawer
          documents={documents}
          drawer={drawer}
          onClose={() => {
            setDrawer(null);
            citationFocus.current?.focus();
          }}
        />
      ) : null}
    </div>
  );
}

function Documents({
  active,
  documents,
  error,
  scopeId,
  onSelect,
  onChange,
  onStatus,
}: {
  active: boolean;
  documents: DocumentRecord[];
  error: string | null;
  scopeId: string;
  onSelect: (documentId: string) => void;
  onChange: () => Promise<void>;
  onStatus: (event: StatusEvent) => void;
}) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploadName, setUploadName] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [replaceId, setReplaceId] = useState<string | null>(null);

  async function sendFile(file: File) {
    if (!acceptedFile(file)) {
      setUploadError("Choose a PDF or DOCX file.");
      return;
    }
    setUploadName(file.name);
    setUploading(true);
    setUploadError(null);
    try {
      await uploadDocument(file);
      setUploadName(null);
      if (inputRef.current) {
        inputRef.current.value = "";
      }
      await onChange();
    } catch (caught) {
      setUploadError(caught instanceof Error ? caught.message : "upload_failed");
    } finally {
      setUploading(false);
    }
  }

  return (
    <section className={active ? "sidebar is-active" : "sidebar"} aria-label="Documents">
      <div className="side-head">
        <h2>
          Documents <span className="count">{documents.length}</span>
        </h2>
        <label className={uploading ? "primary is-disabled" : "primary"} htmlFor={inputId}>
          Upload
          <input
            ref={inputRef}
            id={inputId}
            className="sr-only"
            name="file"
            type="file"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            aria-label="PDF or DOCX"
            disabled={uploading}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                void sendFile(file);
              }
            }}
          />
        </label>
      </div>
      <div
        className="drop"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          const file = event.dataTransfer.files[0];
          if (file && !uploading) {
            void sendFile(file);
          }
        }}
      >
        <p aria-live="polite">{uploading ? `Uploading ${uploadName ?? "file"}…` : uploadName ?? "Drop a PDF or DOCX here"}</p>
      </div>
      {uploadError ? <p className="problem">{readable(uploadError, "The file could not be uploaded.")}</p> : null}
      {error ? <p className="problem">{readable(error, "The document list could not be loaded.")}</p> : null}
      <ul className="doc-list">
        {documents.length === 0 ? <li className="empty-row">No documents yet.</li> : null}
        {documents.map((document) => (
          <DocumentRow
            key={document.id}
            document={document}
            selected={document.id === scopeId}
            onSelect={onSelect}
            onChange={onChange}
            onStatus={onStatus}
            onReplace={() => setReplaceId(document.id)}
          />
        ))}
      </ul>
      {replaceId ? (
        <ReplaceDialog
          document={documents.find((document) => document.id === replaceId) ?? null}
          onClose={() => setReplaceId(null)}
          onChange={onChange}
        />
      ) : null}
    </section>
  );
}

function DocumentRow({
  document,
  selected,
  onSelect,
  onChange,
  onStatus,
  onReplace,
}: {
  document: DocumentRecord;
  selected: boolean;
  onSelect: (documentId: string) => void;
  onChange: () => Promise<void>;
  onStatus: (event: StatusEvent) => void;
  onReplace: () => void;
}) {
  const latest = document.versions[document.versions.length - 1];
  const name = latest?.source_label ?? "Untitled document";
  const status = documentStatus(document);
  const [rowError, setRowError] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuBox, setMenuBox] = useState<{ top: number; left: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (latest === undefined || latest.state === "ready" || latest.state === "failed") {
      return;
    }
    const source = new EventSource(eventsUrl(document.id));
    const onEvent = (event: Event) => {
      const data = JSON.parse((event as MessageEvent).data) as StatusEvent;
      onStatus(data);
      if (data.terminal) {
        source.close();
        void onChange();
      }
    };
    source.addEventListener("status", onEvent);
    return () => source.close();
  }, [document.id, latest?.id, latest?.state, onChange, onStatus]);

  useEffect(() => {
    if (!menuOpen) {
      return;
    }
    function onPointer(event: PointerEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }
    function onKey(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuButton.current?.focus();
      }
    }
    function onScroll() {
      setMenuOpen(false);
    }
    window.document.addEventListener("pointerdown", onPointer);
    window.document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.document.removeEventListener("pointerdown", onPointer);
      window.document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [menuOpen]);

  async function onRetry() {
    if (latest === undefined) {
      return;
    }
    setRowError(null);
    try {
      await retryVersion(document.id, latest.id);
      await onChange();
    } catch (caught) {
      setRowError(caught instanceof Error ? caught.message : "retry_failed");
    }
  }

  const canRetry = latest?.state === "failed" && latest.error_code !== null && RETRYABLE.has(latest.error_code);

  return (
    <li className={selected ? "doc selected" : "doc"}>
      <button
        type="button"
        className="doc-select"
        aria-pressed={selected}
        onClick={() => onSelect(document.id)}
      >
        <FileIcon />
        <span className="doc-copy">
          <span className="doc-name" title={name}>
            {name}
          </span>
          <span className="doc-meta">
            {fileFormat(name)} · {status.label}
            {status.detail ? ` · ${status.detail}` : ""}
          </span>
        </span>
      </button>
      {canRetry ? (
        <button type="button" className="ghost" onClick={() => void onRetry()}>
          Retry
        </button>
      ) : null}
      <div className="menu" ref={menuRef}>
        <button
          ref={menuButton}
          type="button"
          className="icon-button"
          aria-label={`Actions for ${name}`}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => {
            const rect = menuButton.current?.getBoundingClientRect();
            if (rect) {
              const menuHeight = 52;
              const top = window.innerHeight - rect.bottom < menuHeight + 8 ? rect.top - menuHeight - 4 : rect.bottom + 4;
              setMenuBox({ top: Math.max(8, top), left: Math.max(8, rect.right - 168) });
            }
            setMenuOpen((open) => !open);
          }}
        >
          <DotsIcon />
        </button>
        {menuOpen && menuBox ? (
          <div role="menu" className="menu-pop" style={{ top: menuBox.top, left: menuBox.left }}>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                onReplace();
              }}
            >
              Replace
            </button>
          </div>
        ) : null}
      </div>
      {rowError ? <p className="problem">{readable(rowError, "Processing could not be restarted.")}</p> : null}
    </li>
  );
}

function ReplaceDialog({
  document,
  onClose,
  onChange,
}: {
  document: DocumentRecord | null;
  onClose: () => void;
  onChange: () => Promise<void>;
}) {
  const titleId = useId();
  const inputId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const name = document ? labelFor(document) : "Document";

  const closeRefStable = useRef(onClose);
  closeRefStable.current = onClose;

  useEffect(() => {
    const previous = window.document.activeElement instanceof HTMLElement ? window.document.activeElement : null;
    closeRef.current?.focus();
    function onKey(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRefStable.current();
        return;
      }
      if (event.key !== "Tab" || dialogRef.current === null) {
        return;
      }
      const items = focusable(dialogRef.current);
      if (items.length === 0) {
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && window.document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && window.document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.document.addEventListener("keydown", onKey);
    return () => {
      window.document.removeEventListener("keydown", onKey);
      const trigger = window.document.querySelector<HTMLElement>(`[aria-label="${CSS.escape(`Actions for ${name}`)}"]`);
      (trigger ?? previous)?.focus();
    };
  }, [name]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (document === null || file === null || pending) {
      return;
    }
    if (!acceptedFile(file)) {
      setError("Choose a PDF or DOCX file.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      await replaceDocument(document.id, file);
      await onChange();
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "replace_failed");
      setPending(false);
    }
  }

  return (
    <div className="scrim">
      <div
        ref={dialogRef}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div className="dialog-head">
          <h3 id={titleId}>Replace file</h3>
          <button ref={closeRef} type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <p className="dialog-copy" title={name}>
          {name}
        </p>
        <form onSubmit={onSubmit}>
          <label className="ghost file-label" htmlFor={inputId}>
            Choose replacement
            <input
              id={inputId}
              className="sr-only"
              type="file"
              accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </label>
          <p className="file-name" title={file?.name}>
            {file ? file.name : "No file selected"}
          </p>
          {error ? <p className="problem">{readable(error, "The file could not be replaced.")}</p> : null}
          <div className="dialog-actions">
            <button type="button" className="ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="primary" disabled={file === null || pending}>
              {pending ? "Replacing…" : "Replace"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Chat({
  active,
  documents,
  readyCount,
  scopeId,
  turns,
  onScope,
  onTurns,
  onOpenCitation,
}: {
  active: boolean;
  documents: DocumentRecord[];
  readyCount: number;
  scopeId: string;
  turns: Turn[];
  onScope: (documentId: string) => void;
  onTurns: (update: (current: Turn[]) => Turn[]) => void;
  onOpenCitation: (citation: Citation, source: HTMLElement) => void;
}) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const scopeLabel = scopeId === "" ? "All ready documents" : labelFor(documents.find((item) => item.id === scopeId));

  useEffect(() => {
    const node = transcriptRef.current;
    if (node) {
      node.scrollTop = node.scrollHeight;
    }
  }, [turns]);

  function resize(element: HTMLTextAreaElement) {
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 160)}px`;
  }

  async function submit() {
    const question = text.trim();
    if (question.length === 0 || pending) {
      return;
    }
    const turnId = crypto.randomUUID();
    setPending(true);
    setText("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
    onTurns((current) => [
      ...current,
      { id: turnId, text: question, scopeLabel, pending: true, question: null, error: null },
    ]);
    try {
      const result = await askQuestion(question, scopeId || null);
      onTurns((current) =>
        current.map((turn) => (turn.id === turnId ? { ...turn, pending: false, question: result } : turn)),
      );
    } catch (caught) {
      const code = caught instanceof Error ? caught.message : "unexpected";
      onTurns((current) =>
        current.map((turn) => (turn.id === turnId ? { ...turn, pending: false, error: code } : turn)),
      );
    } finally {
      setPending(false);
    }
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    void submit();
  }

  return (
    <section className={active ? "chat is-active" : "chat"} aria-label="Questions">
      <div className="toolbar">
        <label className="scope">
          <span>Search in:</span>
          <select value={scopeId} onChange={(event) => onScope(event.target.value)} aria-label="Search in">
            <option value="">All ready documents</option>
            {documents.map((document) => (
              <option key={document.id} value={document.id}>
                {labelFor(document)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="transcript" ref={transcriptRef}>
        {turns.length === 0 ? (
          <div className="empty-chat">
            <h2>Ask about your documents</h2>
            <p>Answers include citations to the stored source text.</p>
            {readyCount === 0 ? (
              <p>Upload a PDF or DOCX. A question can search a document after it is ready.</p>
            ) : (
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="ghost"
                    onClick={() => {
                      setText(suggestion);
                      requestAnimationFrame(() => {
                        const field = textareaRef.current;
                        if (field === null) {
                          return;
                        }
                        field.focus();
                        resize(field);
                      });
                    }}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <ol className="turns">
            {turns.map((turn) => (
              <li key={turn.id}>
                <article className="user-turn">
                  <p className="who">You</p>
                  <p>{turn.text}</p>
                  <p className="meta">Search in: {turn.scopeLabel}</p>
                </article>
                <article className="assistant-turn">
                  <p className="who">Assistant</p>
                  {turn.pending ? <p className="meta">Searching the documents…</p> : null}
                  {turn.error ? <p className="problem">{readable(turn.error, "The question failed.")}</p> : null}
                  {turn.question ? (
                    <Answer question={turn.question} documents={documents} onOpen={onOpenCitation} />
                  ) : null}
                </article>
              </li>
            ))}
          </ol>
        )}
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <textarea
          ref={textareaRef}
          value={text}
          rows={1}
          maxLength={2000}
          placeholder="Ask a question"
          aria-label="Question"
          onChange={(event) => {
            setText(event.target.value);
            resize(event.currentTarget);
          }}
          onKeyDown={onKeyDown}
        />
        <button type="submit" className="primary" disabled={pending || text.trim().length === 0}>
          Send
        </button>
      </form>
    </section>
  );
}

function Answer({
  question,
  documents,
  onOpen,
}: {
  question: Question;
  documents: DocumentRecord[];
  onOpen: (citation: Citation, source: HTMLElement) => void;
}) {
  if (question.state === "failed") {
    return <p className="problem">{readable(question.error_code ?? "unexpected", "The question failed.")}</p>;
  }
  if (question.outcome === "insufficient_evidence") {
    return (
      <p className="notice">
        {question.answer ?? "The indexed documents do not contain enough evidence to answer."}
      </p>
    );
  }
  return (
    <>
      <p className="answer">{question.answer ?? "No answer text."}</p>
      {question.citations.length > 0 ? (
        <ul className="citations">
          {question.citations.map((citation) => {
            const name = labelFor(documents.find((document) => document.id === citation.document_id));
            return (
              <li key={citation.chunk_id}>
                <button
                  type="button"
                  className="ghost citation"
                  onClick={(event) => onOpen(citation, event.currentTarget)}
                >
                  <span className="cite-name" title={name}>
                    {name}
                  </span>
                  <span>Page {citation.page}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </>
  );
}

function CitationDrawer({
  documents,
  drawer,
  onClose,
}: {
  documents: DocumentRecord[];
  drawer: DrawerState;
  onClose: () => void;
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const citation = drawer.citation;
  const sourceDocument = documents.find((item) => item.id === citation.document_id);
  const name = labelFor(sourceDocument);
  const version = sourceDocument?.versions.find((item) => item.id === citation.version_id);
  const versionLabel = version ? `Version ${version.sequence}` : "Stored version";

  const closeRefStable = useRef(onClose);
  closeRefStable.current = onClose;

  useEffect(() => {
    closeRef.current?.focus();
    function onKey(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRefStable.current();
        return;
      }
      if (event.key !== "Tab" || panelRef.current === null) {
        return;
      }
      const items = focusable(panelRef.current);
      if (items.length === 0) {
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && window.document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && window.document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.document.addEventListener("keydown", onKey);
    return () => window.document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="scrim drawer-scrim">
      <div
        ref={panelRef}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div className="dialog-head">
          <h3 id={titleId} title={name}>
            {name}
          </h3>
          <button ref={closeRef} type="button" className="icon-button" aria-label="Close excerpt" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <p className="meta">
          {versionLabel} · Page {citation.page}
        </p>
        {drawer.error ? <p className="problem">{readable(drawer.error, "The stored excerpt could not be opened.")}</p> : null}
        {drawer.excerpt ? <pre>{drawer.excerpt.excerpt}</pre> : drawer.error ? null : <p className="meta">Opening excerpt…</p>}
        <p>
          <a href={normalizedUrl(citation.document_id, citation.version_id)} target="_blank" rel="noreferrer">
            Open source PDF
          </a>
        </p>
      </div>
    </div>
  );
}

function FileIcon() {
  return (
    <svg className="file-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M7 3.5h6.2L19 9.2V20a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 6 20V5A1.5 1.5 0 0 1 7.5 3.5H7z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path d="M13 3.8V9h5.1" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function DotsIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="6" cy="12" r="1.4" fill="currentColor" />
      <circle cx="12" cy="12" r="1.4" fill="currentColor" />
      <circle cx="18" cy="12" r="1.4" fill="currentColor" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 7l10 10M17 7L7 17" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function documentStatus(document: DocumentRecord): { label: string; detail: string | null } {
  const latest = document.versions[document.versions.length - 1];
  if (latest === undefined) {
    return { label: "No file", detail: null };
  }
  if (latest.state === "failed") {
    const detail = DOCUMENT_ERROR[latest.error_code ?? ""] ?? "Processing failed";
    return { label: "Failed", detail };
  }
  if (latest.state === "ready") {
    return { label: "Ready", detail: null };
  }
  if (latest.state === "queued" || latest.state === "accepted") {
    return { label: "Queued", detail: null };
  }
  return { label: STAGE_LABEL[latest.stage] ?? "Processing", detail: null };
}

function labelFor(document: DocumentRecord | undefined): string {
  if (document === undefined) {
    return "Document";
  }
  return document.versions[document.versions.length - 1]?.source_label ?? "Untitled document";
}

function fileFormat(name: string): string {
  const lower = name.toLowerCase();
  if (lower.endsWith(".docx")) {
    return "DOCX";
  }
  if (lower.endsWith(".pdf")) {
    return "PDF";
  }
  return "File";
}

function acceptedFile(file: File): boolean {
  const lower = file.name.toLowerCase();
  return lower.endsWith(".pdf") || lower.endsWith(".docx");
}

function readable(code: string, fallback: string): string {
  return QUESTION_ERROR[code] ?? DOCUMENT_ERROR[code] ?? fallback;
}

function focusable(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>("button, a[href], input, select, textarea"),
  ).filter((item) => !item.hasAttribute("disabled"));
}
