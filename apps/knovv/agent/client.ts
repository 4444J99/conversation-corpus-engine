import type { Document } from "../src/contracts";
import { Vault } from "../src/vault";

export interface AgentCommand {
  action: "new" | "save" | "read" | "drafts";
  agent: string;
  id?: string;
  parent?: string | null;
  input?: Partial<Document>;
}
export async function executeAgent(
  vault: Vault,
  command: AgentCommand,
): Promise<any> {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/.test(command.agent))
    throw new Error("invalid-agent-name");
  if (command.action === "drafts") return { drafts: await vault.drafts() };
  if (command.action === "read") {
    if (!command.id) throw new Error("document-id-required");
    return vault.open(command.id);
  }
  const input = command.input;
  if (!input || typeof input.body !== "string")
    throw new Error("document-body-required");
  let base: Document;
  if (command.action === "new") {
    if (
      !["wiki", "project", "zettel", "capture"].includes(input.kind ?? "zettel")
    )
      throw new Error("invalid-document-kind");
    base = vault.newDocument(input.kind ?? "zettel");
  } else {
    if (!command.id || command.parent === undefined)
      throw new Error("explicit-parent-required");
    base = (await vault.open(command.id)).document;
  }
  const fields: Partial<Document> = {};
  for (const key of [
    "title",
    "body",
    "summary",
    "sources",
    "links",
    "tags",
    "source_url",
    "relationships",
    "status",
    "logical_id",
    "provenance",
  ] as const) {
    if (input[key] !== undefined) (fields as any)[key] = input[key];
  }
  const document: Document = {
    ...base,
    ...fields,
    author: "agent",
    authorship_basis: "declared",
    last_editor: "agent:" + command.agent,
    updated: new Date().toISOString(),
  };
  const saved = await vault.save(
    document,
    command.action === "new" ? null : command.parent!,
  );
  return { document, revision: saved.revision, state: saved.state };
}
