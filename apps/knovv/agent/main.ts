import { parseArgs } from "node:util";
import { FileStorage, privateRead, privateOutput, noLinks } from "./storage";
import { accessFetcher } from "./transport";
import { executeAgent, type AgentCommand } from "./client";
import { Vault } from "../src/vault";

async function main() {
  process.umask(0o077);
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      package: { type: "string" },
      "passphrase-file": { type: "string" },
      "state-dir": { type: "string" },
      origin: { type: "string" },
      agent: { type: "string" },
      id: { type: "string" },
      parent: { type: "string" },
      input: { type: "string" },
      output: { type: "string" },
      help: { type: "boolean" },
    },
  });
  if (values.help) {
    console.log(
      "cce-knovv agent <new|save|read|drafts> --package FILE --passphrase-file FILE --state-dir DIRECTORY --origin https://HOST --agent NAME --output NEW_FILE [--input JSON_FILE] [--id DOCUMENT] [--parent REVISION|none]\nInput JSON contains title/body and optional sources, links, tags, relationships or status. Files must be private (0600); state must be owned (0700). Login once with cf access login for the application. Agent saves declare attribution and retain conflicting drafts.",
    );
    return;
  }
  const action = positionals[0];
  if (
    positionals.length !== 1 ||
    !["new", "save", "read", "drafts"].includes(action)
  )
    throw new Error("invalid-agent-action");
  for (const key of [
    "package",
    "passphrase-file",
    "state-dir",
    "origin",
    "agent",
    "output",
  ] as const)
    if (!values[key]) throw new Error("required-agent-option-missing");
  // Refuse an existing output before any remote writes.
  await noLinks(values.output!);
  const { lstat } = await import("node:fs/promises");
  if (
    await lstat(values.output!).catch((error) => {
      if (error.code === "ENOENT") return undefined;
      throw error;
    })
  )
    throw new Error("output-exists");
  const pack = JSON.parse(
    (await privateRead(values.package!, 1024 * 1024)).toString(),
  );
  const passphrase /* allow-secret: runtime-only */ = (
    await privateRead(values["passphrase-file"]!, 1024 * 1024)
  ).toString();
  const input = values.input
    ? JSON.parse((await privateRead(values.input)).toString())
    : undefined;
  const fetcher = accessFetcher(values.origin!);
  const storage = await FileStorage.open(values["state-dir"]!);
  const vault = new Vault(storage, fetcher);
  try {
    await vault.unlock(pack, passphrase);
    const command: AgentCommand = {
      action: action as AgentCommand["action"],
      agent: values.agent!,
      id: values.id,
      parent: values.parent === "none" ? null : values.parent,
      input,
    };
    if (
      command.parent !== undefined &&
      command.parent !== null &&
      !/^[0-9a-f]{64}$/.test(command.parent)
    )
      throw new Error("invalid-parent");
    const result = await executeAgent(vault, command);
    await privateOutput(values.output!, result);
    console.log(
      JSON.stringify({
        output: values.output,
        state: result.state ?? "read",
        document: result.document?.id,
      }),
    );
  } finally {
    vault.lock();
    await storage.close();
  }
}
main().catch((error) => {
  const code =
    typeof error?.message === "string" &&
    /^[a-z0-9-]{1,120}$/.test(error.message)
      ? error.message
      : "agent-command-failed";
  console.error(
    JSON.stringify({ error: code, drafts: "retained-in-private-state" }),
  );
  process.exitCode = 1;
});
