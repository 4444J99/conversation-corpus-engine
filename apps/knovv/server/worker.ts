import { api, type Env, json, type Principal } from "./api";
type AccessContext = ExecutionContext & {
  access?: {
    aud: string;
    getIdentity: () => Promise<{ email?: string } | null>;
  };
};
export async function authenticate(
  env: Env,
  ctx: AccessContext,
): Promise<Principal | null> {
  if (!env.ACCESS_AUD || !ctx.access || ctx.access.aud !== env.ACCESS_AUD)
    return null;
  const identity = await ctx.access.getIdentity();
  return identity?.email === env.OWNER_EMAIL ? { email: identity.email } : null;
}
export default {
  async fetch(
    request: Request,
    env: Env,
    ctx: AccessContext,
  ): Promise<Response> {
    let principal: Principal | null = null;
    try {
      principal = await authenticate(env, ctx);
    } catch {
      return json({ error: "authentication-unavailable" }, 503);
    }
    if (!principal) return json({ error: "not-authorized" }, 403);
    const path = new URL(request.url).pathname;
    if (path.startsWith("/api/")) return api(request, env, principal);
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
