import { api, type Env, json, type Principal } from "./api";
import { authenticate } from "./auth";
import type { KnovvJournal } from "./durable";
export { KnovvJournal } from "./durable";
export { authenticate } from "./auth";
type WorkerEnv = Omit<Env, "DB"> & {
  DB?: Env["DB"];
  PUBLISHER_CLIENT_ID?: string;
  ACCESS_ISSUER?: string;
  JOURNAL?: DurableObjectNamespace<KnovvJournal>;
};
type AccessContext = Parameters<typeof authenticate>[1];
export default {
  async fetch(
    request: Request,
    env: WorkerEnv,
    ctx: AccessContext,
  ): Promise<Response> {
    let principal: Principal | null = null;
    try {
      principal = await authenticate(
        env,
        ctx,
        new URL(request.url).pathname,
        request,
      );
    } catch {
      return json({ error: "authentication-unavailable" }, 503);
    }
    if (!principal) return json({ error: "not-authorized" }, 403);
    const path = new URL(request.url).pathname;
    if (path.startsWith("/api/")) {
      if (env.JOURNAL)
        return env.JOURNAL.getByName(env.OWNER_PUBLIC_KEY).serve(request);
      if (env.DB) return api(request, { ...env, DB: env.DB }, principal);
      return json({ error: "storage-not-configured" }, 503);
    }
    const response = await env.ASSETS.fetch(request);
    const headers = new Headers(response.headers);
    headers.set(
      "content-security-policy",
      "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    );
    headers.set("referrer-policy", "no-referrer");
    headers.set("x-content-type-options", "nosniff");
    headers.set("cache-control", "private, no-store");
    return new Response(response.body, { status: response.status, headers });
  },
};
