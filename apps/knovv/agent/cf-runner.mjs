// Reuse the CLI's public programmatic entrypoint and existing authentication.
// This process never returns credentials. Its protocol accepts only the control
// operations needed by the private publisher, and writes ciphertext only.
import { runMain, CliExit } from "cf";
import { createInterface } from "node:readline";
import { openSync, writeSync, closeSync, constants, lstatSync } from "node:fs";
const send = process.stdout.write.bind(process.stdout);
const allowed = (args) =>
  Array.isArray(args) &&
  args.every((a) => typeof a === "string") &&
  ((args.length === 1 && args[0] === "--version") ||
    (args[0] === "d1" && ["query", "list"].includes(args[1])) ||
    (args[0] === "r2" &&
      args[1] === "objects" &&
      ["put", "get"].includes(args[2])) ||
    (args[0] === "access" &&
      args[1] === "curl" &&
      !args.includes("--allow-request") &&
      !args.includes("--ar")));
for await (const line of createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
})) {
  let request;
  try {
    if (line.length > 2 * 1024 * 1024) throw Error();
    request = JSON.parse(line);
  } catch {
    send(
      JSON.stringify({ ok: false, error: "invalid-control-request" }) + "\n",
    );
    continue;
  }
  if (!allowed(request.args)) {
    send(
      JSON.stringify({ ok: false, error: "unsupported-control-operation" }) +
        "\n",
    );
    continue;
  }
  let fd = null,
    length = 0,
    parts = [];
  const capture = (chunk, encoding, callback) => {
    const bytes = Buffer.isBuffer(chunk)
      ? chunk
      : Buffer.from(chunk, typeof encoding === "string" ? encoding : undefined);
    length += bytes.length;
    if (length > 64 * 1024 * 1024) throw Error("control-response-bound");
    if (fd !== null) writeSync(fd, bytes);
    else parts.push(bytes);
    const cb = typeof encoding === "function" ? encoding : callback;
    if (cb) cb();
    return true;
  };
  let ok = true,
    code = 0;
  const priorErr = process.stderr.write;
  try {
    if (request.output) {
      const stat = lstatSync(request.output);
      if (
        !stat.isFile() ||
        stat.isSymbolicLink() ||
        stat.uid !== process.getuid() ||
        stat.mode & 0o077
      )
        throw Error("private-output-required");
      fd = openSync(
        request.output,
        constants.O_WRONLY | constants.O_APPEND | constants.O_NOFOLLOW,
      );
    }
    process.stdout.write = capture;
    process.stderr.write = (_chunk, encoding, callback) => {
      const cb = typeof encoding === "function" ? encoding : callback;
      if (cb) cb();
      return true;
    };
    await runMain(request.args);
  } catch (error) {
    code = error instanceof CliExit ? error.code : 1;
    ok = code === 0;
  } finally {
    process.stdout.write = send;
    process.stderr.write = priorErr;
    if (fd !== null) closeSync(fd);
  }
  send(
    JSON.stringify(
      ok
        ? {
            ok: true,
            bytes: length,
            stdout: fd === null ? Buffer.concat(parts).toString("base64") : "",
          }
        : { ok: false, error: "cloudflare-command-failed", code },
    ) + "\n",
  );
}
process.exit(0);
