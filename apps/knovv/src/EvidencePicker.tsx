import { useState } from "react";
import type { Document, DocumentMeta } from "./contracts";
import { Vault } from "./vault";

export function EvidencePicker({
  vault,
  value,
  onChange,
}: {
  vault: Vault;
  value: Document;
  onChange: (value: Document) => void;
}) {
  const [query, setQuery] = useState(""),
    [results, setResults] = useState<DocumentMeta[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [relation, setRelation] = useState("related");
  async function search() {
    setError("");
    setBusy(true);
    try {
      const found = await vault.baseSearch(query);
      const pages = await Promise.all(
        found.ordinals.slice(0, 20).map((id) => vault.meta(id)),
      );
      setResults(pages.filter((p) => p.id !== value.id));
    } catch {
      setError(
        "Evidence search is unavailable. Existing citations are retained.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function attach(meta: DocumentMeta) {
    setError("");
    try {
      if (meta.kind === "source") {
        const source = await vault.open(meta.id),
          ref = source.document.sources[0];
        if (!ref) throw new Error("missing-source");
        const body = source.document.body,
          term = (query.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [])[0] ?? "";
        const offset = Math.max(0, body.toLowerCase().indexOf(term) - 120),
          length = Math.min(400, body.length - offset);
        const citation = {
          ...ref,
          start: ref.start + offset,
          end: ref.start + offset + length,
        };
        onChange({ ...value, sources: [...value.sources, citation] });
      } else {
        onChange({
          ...value,
          links: [...new Set([...value.links, meta.id])],
          relationships: [
            ...(value.relationships ?? []),
            { type: relation, target: meta.id },
          ],
        });
      }
      setResults([]);
      setQuery("");
    } catch {
      setError(
        "The source passage could not be resolved. No citation was attached.",
      );
    }
  }
  return (
    <details className="evidence-picker">
      <summary>＋ Attach evidence or link a page</summary>
      <p>Find a page or source passage, inspect its preview, then attach it.</p>
      <div className="evidence-search">
        <input
          aria-label="Find evidence"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search your knowledge…"
        />
        <button
          type="button"
          disabled={busy || !query.trim()}
          onClick={() => void search()}
        >
          {busy ? "Finding…" : "Find"}
        </button>
      </div>
      <label>
        Relationship
        <select value={relation} onChange={(e) => setRelation(e.target.value)}>
          {["related", "supports", "contradicts", "extends", "supersedes"].map(
            (r) => (
              <option key={r}>{r}</option>
            ),
          )}
        </select>
      </label>
      {error && <p role="alert">{error}</p>}
      <div>
        {results.map((meta) => (
          <button type="button" key={meta.id} onClick={() => void attach(meta)}>
            <strong>{meta.title}</strong>
            <span>{meta.summary}</span>
            <small>
              {meta.kind === "source"
                ? "Attach a source-bound excerpt"
                : "Link this page"}
            </small>
          </button>
        ))}
      </div>
    </details>
  );
}
