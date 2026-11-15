import { spawn } from "node:child_process";

export function accessFetcher(origin: string) {
  const base = new URL(origin);
  if (
    base.protocol !== "https:" ||
    base.username ||
    base.password ||
    base.search ||
    base.hash ||
    base.pathname !== "/"
  )
    throw new Error("https-application-origin-required");
  const deadline = Date.now() + 120000;
  return async (path: string, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(path, base);
    if (url.origin !== base.origin || !url.pathname.startsWith("/api/"))
      throw new Error("invalid-agent-api-path");
    const args = [
      "access",
      "curl",
      url.href,
      "--",
      "--silent",
      "--show-error",
      "--max-time",
      "55",
      "--max-filesize",
      String(48 * 1024 * 1024),
      "--request",
      init.method ?? "GET",
      "--header",
      "origin: " + base.origin,
      "--write-out",
      "\n%{http_code}",
    ];
    new Headers(init.headers).forEach((value, key) =>
      args.push("--header", key + ": " + value),
    );
    if (init.body != null) args.push("--data-binary", "@-");
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("agent-command-deadline");
    const raw = await new Promise<Buffer>((resolve, reject) => {
      const child = spawn("cf", args, { stdio: ["pipe", "pipe", "pipe"] });
      const chunks: Buffer[] = [];
      let size = 0,
        failed = false;
      const fail = (code: string) => {
        if (failed) return;
        failed = true;
        child.kill("SIGKILL");
        reject(new Error(code));
      };
      const timer = setTimeout(
        () => fail("access-request-deadline"),
        Math.min(60000, remaining),
      );
      child.stdout.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > 48 * 1024 * 1024 + 1024) fail("access-response-bound");
        else chunks.push(chunk);
      });
      child.stderr.resume();
      child.stdin.on("error", () => fail("access-request-failed"));
      child.on("error", () => fail("access-command-unavailable"));
      child.on("close", (code) => {
        clearTimeout(timer);
        if (failed) return;
        if (code !== 0) {
          reject(new Error("access-authentication-or-request-failed"));
          return;
        }
        resolve(Buffer.concat(chunks));
      });
      if (init.body instanceof ArrayBuffer)
        child.stdin.end(Buffer.from(init.body));
      else if (typeof init.body === "string") child.stdin.end(init.body);
      else if (init.body == null) child.stdin.end();
      else fail("unsupported-agent-request-body");
    });
    const split = raw.lastIndexOf(10),
      status = Number(raw.subarray(split + 1).toString());
    if (split < 0 || !Number.isInteger(status) || status < 200 || status > 599)
      throw new Error("invalid-access-response");
    return new Response(new Uint8Array(raw.subarray(0, split)), { status });
  };
}
