import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiError, Vault, type OpenDocument } from "./vault";
import { BrowserStorage } from "./storage";
import type {
  DevicePackage,
  Document,
  DocumentMeta,
  JournalEntry,
} from "./contracts";
import { terms } from "./search";
import { EvidencePicker } from "./EvidencePicker";
import { headingIds } from "./markdown";

const screens = [
  ["home", "◉", "Workspace"],
  ["wiki", "▤", "Wiki"],
  ["project", "◇", "Projects"],
  ["zettel", "▫", "Atomic notes"],
  ["source", "⌁", "Sources"],
  ["capture", "↙", "Capture"],
  ["changes", "↻", "Recent changes"],
  ["drafts", "✎", "Local drafts"],
  ["health", "✓", "Source status"],
];
const explain = (error: unknown) => {
  const code = error instanceof Error ? error.message : "";
  const messages: Record<string, string> = {
    "credential-unlock-failed":
      "The device package could not be unlocked. Check the passphrase.",
    "device-not-enrolled":
      "This device is not currently enrolled. An owner must authorize its key.",
    "rollback-or-invalid-publication":
      "The publication does not match your trusted checkpoint. It has not been opened.",
    "publication-unavailable":
      "No knowledge publication is available yet. Publish a verified CCE bundle first.",
    "storage-unavailable":
      "Remote storage is unavailable. Your local encrypted draft is retained.",
    "storage-budget-exceeded":
      "The storage budget has been reached. Your local encrypted draft is retained.",
    "revision-conflict":
      "Another revision was saved first. Your draft is retained; compare it with the current version.",
    "not-authorized": "Your sign-in is not authorized for this workspace.",
    "historical-epoch-not-enrolled":
      "This revision uses an older encryption epoch that this device cannot unlock.",
    "device-storage-unavailable":
      "Browser storage is unavailable. Saved drafts cannot be retained on this device.",
  };
  return (
    messages[code] ??
    "The operation could not finish. Your existing sources have not changed."
  );
};
const excerpt = (text: string) => text.replace(/[#*`\[\]]/g, "").slice(0, 180);
const parentOf = (opened: OpenDocument | null) =>
  opened?.local
    ? (opened.revision?.parent ?? null)
    : (opened?.revision?.id ?? null);
function date(value: string) {
  if (!value) return "Date not supplied";
  const n = new Date(value);
  return Number.isNaN(n.getTime()) ? value : n.toLocaleDateString();
}

export function App() {
  const [storage] = useState(() => new BrowserStorage());
  const [vault] = useState(() => new Vault(storage));
  const [unlocked, setUnlocked] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [packText, setPackText] = useState(""),
    [passphrase, setPassphrase] = useState(""),
    [hasPack, setHasPack] = useState(false);
  const [screen, setScreen] = useState("home"),
    [query, setQuery] = useState(""),
    [hits, setHits] = useState<DocumentMeta[]>([]),
    [searchInfo, setSearchInfo] = useState("");
  const [opened, setOpened] = useState<OpenDocument | null>(null),
    [editing, setEditing] = useState(false),
    [edited, setEdited] = useState<Document | null>(null),
    [dirty, setDirty] = useState(false);
  const [drafts, setDrafts] = useState<OpenDocument[]>([]),
    [history, setHistory] = useState<JournalEntry[]>([]),
    [showHistory, setShowHistory] = useState(false),
    [conflict, setConflict] = useState<OpenDocument | null>(null);
  const [version, setVersion] = useState(0),
    [backlinks, setBacklinks] = useState<DocumentMeta[]>([]);
  const [toc, setToc] = useState<{ id: string; text: string }[]>([]);
  const worker = useRef<Worker | null>(null),
    requestId = useRef(0),
    suspend = useRef(false);
  const current = useRef<{
    edited: Document | null;
    opened: OpenDocument | null;
    dirty: boolean;
  }>({ edited: null, opened: null, dirty: false });
  current.current = { edited, opened, dirty };
  useEffect(() => {
    if (!unlocked || editing) {
      setToc([]);
      return;
    }
    setToc(
      Array.from(
        window.document.querySelectorAll(
          ".prose h1[id],.prose h2[id],.prose h3[id],.prose h4[id]",
        ),
      ).map((node) => ({ id: node.id, text: node.textContent ?? "" })),
    );
  }, [unlocked, editing, opened?.document.body]);
  useEffect(() => {
    storage
      .get<DevicePackage>("device-package")
      .then((p) => setHasPack(!!p))
      .catch(() => setError("Encrypted device storage is unavailable."));
    return () => {
      worker.current?.terminate();
      vault.lock();
    };
  }, [storage, vault]);
  async function guarded(work: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await work();
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }
  async function unlock() {
    await guarded(async () => {
      const pack = packText.trim()
        ? JSON.parse(packText)
        : await storage.get<DevicePackage>("device-package");
      if (!pack) throw new Error("invalid-device-package");
      const password = passphrase; // allow-secret: runtime-only
      setPassphrase("");
      await vault.unlock(pack, password);
      setOpened(null);
      setEdited(null);
      setEditing(false);
      setDirty(false);
      setPackText("");
      const w = new Worker(new URL("./search.worker.ts", import.meta.url), {
        type: "module",
      });
      w.postMessage({ type: "init", ...vault.searchWorkerState() });
      worker.current = w;
      setUnlocked(true);
      setDrafts(await vault.drafts());
      setVersion((v) => v + 1);
    });
  }
  async function lock() {
    await guarded(async () => {
      const state = current.current;
      if (state.dirty && state.edited)
        await vault.stage(state.edited, parentOf(state.opened));
      worker.current?.terminate();
      worker.current = null;
      requestId.current++;
      vault.lock();
      setUnlocked(false);
      setOpened(null);
      setEdited(null);
      setEditing(false);
      setDirty(false);
      setHits([]);
      setHistory([]);
      setDrafts([]);
      setConflict(null);
      setBacklinks([]);
      setQuery("");
      setNotice("");
      setError("");
      setHasPack(true);
    });
  }
  async function open(id: string) {
    requestId.current++;
    setQuery("");
    await guarded(async () => {
      const state = current.current;
      if (state.dirty && state.edited)
        await vault.stage(state.edited, parentOf(state.opened));
      const value = await vault.open(id);
      setOpened(value);
      setEdited(null);
      setEditing(false);
      setDirty(false);
      setShowHistory(false);
      setConflict(null);
      setBacklinks([]);
      location.hash = "/document/" + id;
      setScreen("document");
      setBacklinks(vault.backlinks(id));
    });
  }
  async function navigate(next: string) {
    if (dirty && edited) await vault.stage(edited, parentOf(opened));
    setDirty(false);
    setScreen(next);
    setOpened(null);
    setEdited(null);
    setEditing(false);
    setQuery("");
    setHits([]);
    location.hash = "/" + next;
    if (next === "drafts") setDrafts(await vault.drafts());
  }
  useEffect(() => {
    if (!unlocked) return;
    const route = () => {
      const match = location.hash.match(/^#\/document\/([0-9a-f]{64})$/);
      if (match) {
        if (current.current.opened?.document.id !== match[1])
          void open(match[1]);
      } else {
        const page = location.hash.replace(/^#\//, "");
        if (screens.some(([id]) => id === page)) setScreen(page);
      }
    };
    route();
    window.addEventListener("hashchange", route);
    const timer = setInterval(() => {
      if (window.document.visibilityState !== "visible" || suspend.current)
        return;
      suspend.current = true;
      const generation = vault.publication!.generation;
      vault
        .sync()
        .then(() => {
          if (vault.publication?.generation !== generation)
            worker.current?.postMessage({
              type: "init",
              ...vault.searchWorkerState(),
            });
          setVersion((v) => v + 1);
        })
        .catch((e) => setError(explain(e)))
        .finally(() => {
          suspend.current = false;
        });
    }, 5000);
    return () => {
      clearInterval(timer);
      window.removeEventListener("hashchange", route);
    };
  }, [unlocked]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (current.current.dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);
  useEffect(() => {
    if (!unlocked || !dirty || !edited) return;
    const timer = setTimeout(() => {
      vault.stage(edited, parentOf(opened)).catch((e) => setError(explain(e)));
    }, 1000);
    return () => clearTimeout(timer);
  }, [edited, dirty, opened, unlocked, vault]);
  useEffect(() => {
    if (!unlocked || !query.trim()) {
      setHits([]);
      setSearchInfo("");
      return;
    }
    const id = ++requestId.current,
      timer = setTimeout(async () => {
        setSearchInfo("Searching encrypted indexes…");
        setScreen("search");
        const finish = async (result: {
          ordinals: number[];
          total: number;
          plan: string;
          truncated: boolean;
        }) => {
          if (id !== requestId.current) return;
          try {
            const changed = new Set(vault.recent.map((d) => d.id)),
              loaded: DocumentMeta[] = [];
            for (const ordinal of result.ordinals) {
              const meta = await vault.meta(ordinal);
              if (!changed.has(meta.id)) loaded.push(meta);
              if (loaded.length >= 40) break;
            }
            const overlays = (await vault.overlaySearch(query)).map((d) => ({
              ...d,
              ordinal: -1,
              object: "",
            }));
            let all = [...overlays, ...loaded];
            if (query.trim().startsWith('"') && query.trim().endsWith('"')) {
              const phrase = query.trim().slice(1, -1).toLowerCase();
              const checked: DocumentMeta[] = [];
              for (const meta of all) {
                const d = await vault.open(meta.id);
                if (
                  (d.document.title + " " + d.document.body)
                    .toLowerCase()
                    .includes(phrase)
                )
                  checked.push(meta);
              }
              all = checked;
            }
            if (id !== requestId.current) return;
            setHits(all);
            setSearchInfo(
              `${all.length} results shown · ${result.plan === "or" ? "broadened to any term" : "all query terms"}${result.total > all.length ? ` · ${result.total} indexed candidates` : ""}${result.truncated ? " · bounded results" : ""}`,
            );
          } catch (e) {
            setError(explain(e));
            setSearchInfo("Search did not finish.");
          }
        };
        if (worker.current && !vault.offline) {
          worker.current.onmessage = (event) => {
            if (event.data.id !== id) return;
            if (event.data.error) {
              setError(event.data.error);
              setSearchInfo("Search did not finish.");
            } else void finish(event.data.result);
          };
          worker.current.postMessage({ type: "search", id, query });
        } else {
          try {
            await finish(await vault.baseSearch(query));
          } catch (e) {
            setError(explain(e));
          }
        }
      }, 250);
    return () => clearTimeout(timer);
  }, [query, unlocked, version]);
  function edit(value: Document) {
    setEdited({ ...value });
    setEditing(true);
    setDirty(false);
    setConflict(null);
  }
  async function create(kind: Document["kind"] = "capture") {
    await navigate("document");
    const d = vault.newDocument(kind);
    setOpened({ document: d, revision: null });
    edit(d);
  }
  async function save() {
    if (!edited) return;
    await guarded(async () => {
      const doc = {
        ...edited,
        title: edited.title.trim() || "Untitled note",
        summary: edited.summary || excerpt(edited.body),
        updated: new Date().toISOString(),
      };
      try {
        const result = await vault.save(doc, parentOf(opened));
        setDirty(false);
        setEdited(doc);
        setOpened({
          document: doc,
          revision: result.revision,
          local: result.state === "local",
        });
        setNotice(
          result.state === "remote"
            ? "Saved and synced. Archive snapshot pending."
            : "Saved as an encrypted local draft. Remote sync is pending.",
        );
        setVersion((v) => v + 1);
        setDrafts(await vault.drafts());
      } catch (e) {
        if (e instanceof ApiError && e.status === 409)
          setConflict(await vault.open(doc.id));
        throw e;
      }
    });
  }
  async function fileCapture(file: File) {
    if (file.size > 4 * 1024 * 1024) {
      setError("Choose a file of 4 MiB or less.");
      return;
    }
    await create("capture");
    let d = vault.newDocument("capture");
    d.title = file.name;
    const text =
      /^(text\/|application\/(json|xml))/.test(file.type) ||
      /\.(md|txt|csv|json|log|yaml|yml)$/i.test(file.name);
    d.body = text
      ? await file.text()
      : `Captured file: ${file.name}\n\nThe original bytes are preserved as an encrypted attachment. Text extraction is pending source-adapter processing.`;
    d = await vault.attachFile(
      d,
      new Uint8Array(await file.arrayBuffer()),
      file.name,
      file.type || "application/octet-stream",
    );
    d.summary = excerpt(d.body);
    setOpened({ document: d, revision: null });
    setEdited(d);
    setDirty(true);
  }
  function documentCards(docs: DocumentMeta[]) {
    return docs.length ? (
      <div className="cards">
        {docs.map((d) => (
          <button
            className="note-card"
            key={d.id}
            onClick={() => void open(d.id)}
          >
            <span className="eyebrow">
              {d.kind} <span>↗</span>
            </span>
            <h3>{d.title}</h3>
            <p>
              {d.summary || "Open this page to read its contents and evidence."}
            </p>
            <small>
              {d.author || "unknown"} · {d.status}
            </small>
          </button>
        ))}
      </div>
    ) : (
      <div className="empty">
        <span>▫</span>
        <h3>No pages in this view yet</h3>
        <p>Create a note or publish the corresponding CCE material.</p>
        <button
          className="primary"
          onClick={() =>
            void create(
              screen === "project"
                ? "project"
                : screen === "zettel"
                  ? "zettel"
                  : "wiki",
            )
          }
        >
          Create a page
        </button>
      </div>
    );
  }

  if (!unlocked)
    return (
      <div className="locked-shell">
        <div className="lock-brand">
          knovv<span>✳</span>
        </div>
        <main className="unlock">
          <div className="eyebrow">YOUR PRIVATE KNOWLEDGE WORKSPACE</div>
          <h1>
            Unlock your
            <br />
            <em>knowledge.</em>
          </h1>
          <p>
            Your device decrypts your library. Import its protected enrollment
            package and enter your passphrase.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void unlock();
            }}
          >
            <label className="file-control">
              Import device package
              <input
                type="file"
                accept=".json,.knovv"
                onChange={(e) =>
                  void e.target.files?.[0]?.text().then(setPackText)
                }
              />
            </label>
            <details>
              <summary>Or paste an enrollment package</summary>
              <textarea
                aria-label="Enrollment package"
                value={packText}
                onChange={(e) => setPackText(e.target.value)}
                placeholder="Protected device package JSON"
              />
            </details>
            {hasPack && !packText && (
              <p className="hint">
                An encrypted device package is saved on this browser.
              </p>
            )}
            <label>
              Passphrase
              <input
                type="password"
                autoComplete="current-password"
                value={passphrase}
                onChange={(e) => setPassphrase(e.target.value)}
                required
              />
            </label>
            <button className="primary" disabled={busy}>
              {busy ? "Unlocking…" : "Unlock workspace ↗"}
            </button>
          </form>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="lock-foot">
            Owner access · Enrolled devices · Encrypted storage
          </div>
        </main>
        <aside className="unlock-aside">
          <div className="orbit">
            <div>Sources</div>
            <div>Ideas</div>
            <div>Projects</div>
            <strong>knovv</strong>
          </div>
          <p>
            Preserve the source.
            <br />
            Connect the fragments.
            <br />
            Let understanding compound.
          </p>
        </aside>
      </div>
    );
  const updated = new Set(vault.recent.map((d) => d.id));
  const navigation = [
      ...vault.recent
        .filter((d) => d.kind !== "source")
        .map((d) => ({ ...d, ordinal: -1, object: "" })),
      ...vault.manifest!.navigation.filter((d) => !updated.has(d.id)),
    ],
    shown =
      screen === "home"
        ? navigation.slice(0, 12)
        : navigation.filter((p) =>
            screen === "wiki" ? p.kind === "wiki" : p.kind === screen,
          );
  const document = opened?.document,
    content = editing ? edited : document;
  return (
    <div className="app-shell">
      <aside className="rail">
        <button className="brand" onClick={() => void navigate("home")}>
          knovv<span>✳</span>
        </button>
        <div className="rail-caption">THE KNOWLEDGE WORKSPACE</div>
        <nav>
          {screens.map(([id, symbol, label]) => (
            <button
              key={id}
              className={screen === id ? "active" : ""}
              onClick={() => void navigate(id)}
            >
              <span>{symbol}</span>
              {label}
              {id === "drafts" && drafts.length > 0 && (
                <small>{drafts.length}</small>
              )}
            </button>
          ))}
        </nav>
        <div className="rail-bottom">
          <p>
            <span
              className={"status-dot " + (vault.offline ? "offline" : "")}
            />
            {vault.offline
              ? "Offline · cached knowledge"
              : "Connected · encrypted sync"}
          </p>
          <small>
            {vault.manifest!.count.toLocaleString()} published passages and
            pages
          </small>
          <button onClick={() => void lock()}>Lock workspace ↗</button>
        </div>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <span>
            knovv /{" "}
            <b>
              {screen === "document"
                ? document?.kind
                : (screens.find(([id]) => id === screen)?.[2] ?? "Search")}
            </b>
          </span>
          <div>
            <button
              onClick={() =>
                void guarded(async () => {
                  await vault.sync();
                  setVersion((v) => v + 1);
                  setNotice(
                    vault.offline ? "Using cached knowledge." : "Up to date.",
                  );
                })
              }
            >
              ↻ Refresh
            </button>
            <span className="private">● Private</span>
          </div>
        </header>
        <div className="searchbar">
          <span>⌕</span>
          <input
            type="search"
            aria-label="Search your knowledge"
            placeholder="Find an idea, passage, project, or decision…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="primary" onClick={() => void create()}>
            ＋ Capture
          </button>
        </div>
        {error && (
          <div role="alert" className="error">
            {error}
            <button onClick={() => setError("")} aria-label="Dismiss error">
              ×
            </button>
          </div>
        )}
        {notice && (
          <div role="status" className="notice">
            {notice}
            <button
              onClick={() => setNotice("")}
              aria-label="Dismiss notification"
            >
              ×
            </button>
          </div>
        )}
        <div className="page">
          {["home", "wiki", "project", "zettel", "source"].includes(screen) && (
            <>
              <div className="page-heading">
                <div>
                  <div className="eyebrow">
                    {screen === "home"
                      ? "A LIVING BODY OF KNOWLEDGE"
                      : screen === "zettel"
                        ? "ONE IDEA · ITS SOURCES · ITS CONNECTIONS"
                        : "YOUR KNOWLEDGE"}
                  </div>
                  <h1>
                    {screen === "home"
                      ? "Pick up a thread."
                      : screens.find(([id]) => id === screen)?.[2]}
                  </h1>
                  <p>
                    {screen === "home"
                      ? "Find what matters, follow the evidence, and leave understanding clearer."
                      : screen === "source"
                        ? "Search the library to locate source passages. Original source revisions remain intact."
                        : "Readable pages linked to the material behind them."}
                  </p>
                </div>
                <button
                  onClick={() =>
                    void create(
                      screen === "project"
                        ? "project"
                        : screen === "zettel"
                          ? "zettel"
                          : "wiki",
                    )
                  }
                >
                  ＋ New{" "}
                  {screen === "project"
                    ? "project"
                    : screen === "zettel"
                      ? "atomic note"
                      : "page"}
                </button>
              </div>
              {screen === "home" && (
                <div className="stats">
                  <div>
                    <strong>{vault.manifest!.count.toLocaleString()}</strong>
                    <span>Published units</span>
                  </div>
                  <div>
                    <strong>{navigation.length}</strong>
                    <span>Wiki and atomic pages</span>
                  </div>
                  <div>
                    <strong>{vault.recent.length}</strong>
                    <span>Synced edited pages</span>
                  </div>
                  <div>
                    <strong>{drafts.length}</strong>
                    <span>Encrypted local drafts</span>
                  </div>
                </div>
              )}
              {documentCards(shown)}
            </>
          )}
          {screen === "search" && (
            <>
              <div className="eyebrow">PASSAGE SEARCH</div>
              <h1>Following “{query}”</h1>
              <p className="search-info" aria-live="polite">
                {searchInfo}
              </p>
              {hits.length ? (
                documentCards(hits)
              ) : (
                <p className="empty-copy">
                  {searchInfo.startsWith("Searching")
                    ? "Finding the relevant fragments…"
                    : "No matching passages are available for this query."}
                </p>
              )}
            </>
          )}
          {screen === "capture" && (
            <>
              <div className="eyebrow">RETURN SOMETHING USEFUL</div>
              <h1>Capture a thought.</h1>
              <p>
                Keep an idea, paste a source passage, or record a reference for
                later processing.
              </p>
              <div className="capture-options">
                <button onClick={() => void create("capture")}>
                  <strong>✎ Write or paste</strong>
                  <span>A note, quotation, or URL reference.</span>
                </button>
                <label className="file-control">
                  <strong>↙ Import a file</strong>
                  <span>Keep original bytes · up to 4 MiB</span>
                  <input
                    type="file"
                    onChange={(e) =>
                      e.target.files?.[0] && void fileCapture(e.target.files[0])
                    }
                  />
                </label>
              </div>
            </>
          )}
          {screen === "drafts" && (
            <>
              <div className="eyebrow">ENCRYPTED ON THIS DEVICE</div>
              <h1>Local drafts</h1>
              <p>
                These changes remain on this device until explicitly saved and
                synced.
              </p>
              <div className="cards">
                {drafts.map((d) => (
                  <button
                    className="note-card"
                    key={d.document.id}
                    onClick={() => {
                      setOpened(d);
                      setEdited(d.document);
                      setEditing(true);
                      setDirty(true);
                      setScreen("document");
                    }}
                  >
                    <span className="eyebrow">Pending</span>
                    <h3>{d.document.title || "Untitled note"}</h3>
                    <p>{excerpt(d.document.body)}</p>
                  </button>
                ))}
              </div>
              {!drafts.length && <p className="empty-copy">No local drafts.</p>}
            </>
          )}
          {screen === "changes" && (
            <>
              <div className="eyebrow">REVISION HISTORY</div>
              <h1>Recent changes</h1>
              {documentCards(
                vault.recent.map((d) => ({ ...d, ordinal: -1, object: "" })),
              )}
            </>
          )}
          {screen === "health" && (
            <>
              <div className="eyebrow">COVERAGE AND FRESHNESS</div>
              <h1>Source status</h1>
              <p>
                Publication and source refresh are measured separately. A saved
                edit appears immediately; an archive snapshot is a separate
                recovery operation.
              </p>
              <div className="receipt-list">
                {vault.manifest!.receipts.map((r, i) => (
                  <article key={i}>
                    <h3>{String(r.source)}</h3>
                    <strong>{String(r.status)}</strong>
                    <dl>
                      {Object.entries(r)
                        .filter(([k]) => !["source", "status"].includes(k))
                        .map(([k, v]) => (
                          <div key={k}>
                            <dt>{k}</dt>
                            <dd>
                              {typeof v === "object"
                                ? JSON.stringify(v)
                                : String(v)}
                            </dd>
                          </div>
                        ))}
                    </dl>
                  </article>
                ))}
              </div>
              <p className="notice">
                A URL reference is captured when you save it. Article-body
                clipping, browser-history refresh, Notes, and Google Docs
                ingestion require their source adapters and report their own
                results.
              </p>
            </>
          )}
          {screen === "document" && content && (
            <article className="document">
              <div className="document-top">
                <span className="eyebrow">
                  {content.kind} / {content.status}
                </span>
                <div>
                  {document?.kind !== "source" && !editing && (
                    <button onClick={() => edit(document!)}>✎ Edit</button>
                  )}
                  <button
                    onClick={() =>
                      void guarded(async () => {
                        setHistory(await vault.history(content.id));
                        setShowHistory(!showHistory);
                      })
                    }
                  >
                    ↻ History
                  </button>
                </div>
              </div>
              {editing && edited ? (
                <>
                  <label className="sr-only" htmlFor="title">
                    Title
                  </label>
                  <input
                    className="title-editor"
                    id="title"
                    value={edited.title}
                    placeholder="Give this idea a title"
                    onChange={(e) => {
                      setEdited({ ...edited, title: e.target.value });
                      setDirty(true);
                    }}
                  />
                  <div className="editor-meta">
                    <label>
                      Page type
                      <select
                        value={edited.kind}
                        onChange={(e) => {
                          setEdited({
                            ...edited,
                            kind: e.target.value as Document["kind"],
                          });
                          setDirty(true);
                        }}
                      >
                        {["wiki", "project", "zettel", "capture"].map((k) => (
                          <option key={k}>{k}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Status
                      <select
                        value={edited.status}
                        onChange={(e) => {
                          setEdited({ ...edited, status: e.target.value });
                          setDirty(true);
                        }}
                      >
                        {["draft", "stable", "superseded", "archived"].map(
                          (k) => (
                            <option key={k}>{k}</option>
                          ),
                        )}
                      </select>
                    </label>
                    <label>
                      Reference URL
                      <input
                        value={edited.source_url ?? ""}
                        type="url"
                        placeholder="https://…"
                        onChange={(e) => {
                          setEdited({ ...edited, source_url: e.target.value });
                          setDirty(true);
                        }}
                      />
                    </label>
                  </div>
                  <textarea
                    className="body-editor"
                    aria-label="Page content"
                    value={edited.body}
                    placeholder="Write the idea. Link its evidence. Leave the next question clear."
                    onChange={(e) => {
                      setEdited({ ...edited, body: e.target.value });
                      setDirty(true);
                    }}
                  />
                  <label>
                    Authorship
                    <select
                      value={edited.author}
                      onChange={(e) => {
                        setEdited({
                          ...edited,
                          author: e.target.value,
                          authorship_basis: "declared",
                        });
                        setDirty(true);
                      }}
                    >
                      {[
                        "unknown",
                        "owner",
                        "agent",
                        "third_party",
                        "mixed",
                      ].map((author) => (
                        <option key={author}>{author}</option>
                      ))}
                    </select>
                  </label>
                  <EvidencePicker
                    vault={vault}
                    value={edited}
                    onChange={(value) => {
                      setEdited(value);
                      setDirty(true);
                    }}
                  />
                  <div className="editor-actions">
                    <span>
                      {dirty
                        ? "Editing · encrypted draft saved after a short pause"
                        : "No pending edits"}{" "}
                      · Markdown supported
                    </span>
                    <button
                      className="primary"
                      disabled={busy}
                      onClick={() => void save()}
                    >
                      {busy ? "Saving…" : "Save and sync ↗"}
                    </button>
                  </div>
                  {conflict && (
                    <section className="conflict">
                      <h3>A newer revision exists</h3>
                      <p>
                        Your draft is retained below. Compare the current
                        version, reconcile your text, then save a merged
                        revision.
                      </p>
                      <pre>{conflict.document.body}</pre>
                      <button
                        onClick={() => {
                          setOpened(conflict);
                          setConflict(null);
                          setError("");
                          setDirty(true);
                          setNotice(
                            "Your draft is preserved in the editor. The next save will extend the current remote revision.",
                          );
                        }}
                      >
                        Use current revision as the merge parent
                      </button>
                    </section>
                  )}
                </>
              ) : (
                <>
                  <h1>{content.title}</h1>
                  <div className="byline">
                    {content.author || "Unknown author"} ·{" "}
                    {content.authorship_basis} ·{" "}
                    {date(content.updated || content.created)}
                  </div>
                  {toc.length > 1 && (
                    <details className="toc">
                      <summary>On this page</summary>
                      {toc.map((item) => (
                        <button
                          key={item.id}
                          onClick={() =>
                            window.document
                              .getElementById(item.id)
                              ?.scrollIntoView({ behavior: "smooth" })
                          }
                        >
                          {item.text}
                        </button>
                      ))}
                    </details>
                  )}
                  <div className="prose">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm, headingIds]}
                      components={{
                        a: ({ href, children }) =>
                          href?.startsWith("#/document/") ? (
                            <button
                              className="inline-link"
                              onClick={() => void open(href.split("/").at(-1)!)}
                            >
                              {children}
                            </button>
                          ) : (
                            <a
                              href={href}
                              target="_blank"
                              rel="noreferrer noopener"
                            >
                              {children} ↗
                            </a>
                          ),
                      }}
                    >
                      {content.body}
                    </ReactMarkdown>
                  </div>
                  {content.source_url && (
                    <a
                      className="reference"
                      href={content.source_url}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      Open original reference ↗
                    </a>
                  )}
                </>
              )}
              {showHistory && (
                <section className="history">
                  <h2>Saved revisions</h2>
                  {content.source_history?.map((item) => (
                    <button
                      key={item.version}
                      onClick={() => void open(item.id)}
                    >
                      Source revision {item.version} ↗
                    </button>
                  ))}
                  {history.length ? (
                    history.map((entry) => (
                      <button
                        key={entry.seq}
                        onClick={() =>
                          void guarded(async () => {
                            const d = await vault.historical(entry.envelope);
                            setOpened({
                              document: d,
                              revision: entry.envelope,
                            });
                            setEditing(false);
                            setNotice(
                              "Viewing a saved revision. Editing it will preserve newer revisions and require conflict resolution.",
                            );
                          })
                        }
                      >
                        Revision {entry.seq} · {entry.envelope.id.slice(0, 12)}{" "}
                        · encrypted epoch {entry.envelope.epoch}
                      </button>
                    ))
                  ) : (
                    <p>
                      This page is in the published corpus. No browser edits
                      have been saved yet.
                    </p>
                  )}
                </section>
              )}
              <section className="evidence">
                <h2>Evidence and connections</h2>
                {content.attachments?.map((ref) => (
                  <button
                    key={ref.object}
                    onClick={() =>
                      void guarded(async () => {
                        const bytes = await vault.attachment(ref),
                          url = URL.createObjectURL(
                            new Blob([new Uint8Array(bytes).buffer], {
                              type: ref.media_type,
                            }),
                          ),
                          link = window.document.createElement("a");
                        link.href = url;
                        link.download = ref.name;
                        link.click();
                        setTimeout(() => URL.revokeObjectURL(url), 1000);
                      })
                    }
                  >
                    Download {ref.name} · {ref.size.toLocaleString()} bytes ↙
                  </button>
                ))}
                {content.sources.map((s, i) => (
                  <button key={i} onClick={() => void open(vault.sourceId(s))}>
                    {s.uid} · revision {s.version} · message {s.message + 1} ·
                    characters {s.start}–{s.end} ↗
                  </button>
                ))}
                {content.links.map((id) => (
                  <button key={id} onClick={() => void open(id)}>
                    {content.relationships?.find((r) => r.target === id)
                      ?.type ?? "Related"}{" "}
                    ·{" "}
                    {navigation.find((p) => p.id === id)?.title ??
                      "Open linked page"}{" "}
                    ↗
                  </button>
                ))}
                {!content.sources.length && !content.links.length && (
                  <p>
                    No source citations or page links have been attached yet.
                  </p>
                )}
                <h3>Backlinks</h3>
                {backlinks.length ? (
                  backlinks.map((p) => (
                    <button key={p.id} onClick={() => void open(p.id)}>
                      {p.title} ↗
                    </button>
                  ))
                ) : (
                  <p>No published wiki backlinks are recorded for this page.</p>
                )}
                <small className="document-id">Page ID: {content.id}</small>
              </section>
            </article>
          )}
        </div>
        <footer>
          KNOVV · SOURCE → IDEA → UNDERSTANDING → RETURN{" "}
          <span>
            {vault.offline
              ? "Local encrypted cache"
              : "Device-decrypted workspace"}
          </span>
        </footer>
      </main>
    </div>
  );
}
