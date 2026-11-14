import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";
import type { Env, Principal } from "./api";
type AccessContext = ExecutionContext & {
  access?: {
    aud: string;
    getIdentity: () => Promise<{
      email?: string;
      service_token_status?: boolean;
      service_token_id?: string;
    } | null>;
  };
};
type AuthEnv = Pick<Env, "ACCESS_AUD" | "OWNER_EMAIL"> & {
  PUBLISHER_CLIENT_ID?: string;
  ACCESS_ISSUER?: string;
};
let remote: { issuer: string; keys: JWTVerifyGetKey } | undefined;
export async function jwtPrincipal(
  raw: string,
  env: AuthEnv,
  path: string,
  keys?: JWTVerifyGetKey,
): Promise<Principal | null> {
  if (
    !env.ACCESS_AUD ||
    !env.ACCESS_ISSUER ||
    !/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(env.ACCESS_ISSUER) ||
    raw.length > 16384
  )
    return null;
  if (!keys) {
    if (remote?.issuer !== env.ACCESS_ISSUER)
      remote = {
        issuer: env.ACCESS_ISSUER,
        keys: createRemoteJWKSet(
          new URL(env.ACCESS_ISSUER + "/cdn-cgi/access/certs"),
        ),
      };
    keys = remote.keys;
  }
  const { payload } = await jwtVerify(raw, keys, {
    issuer: env.ACCESS_ISSUER,
    audience: env.ACCESS_AUD,
    algorithms: ["RS256"],
    requiredClaims: ["exp", "iat", "iss", "aud"],
    clockTolerance: 5,
  });
  if (payload.type !== "app") return null;
  if (
    path === "/api/control" &&
    env.PUBLISHER_CLIENT_ID &&
    payload.common_name === env.PUBLISHER_CLIENT_ID
  )
    return { email: env.OWNER_EMAIL };
  return payload.email === env.OWNER_EMAIL ? { email: env.OWNER_EMAIL } : null;
}
export async function authenticate(
  env: AuthEnv,
  ctx: AccessContext,
  path = "/",
  request?: Request,
): Promise<Principal | null> {
  if (!env.ACCESS_AUD) return null;
  if (!ctx.access) {
    const raw = request?.headers.get("Cf-Access-Jwt-Assertion");
    return raw ? jwtPrincipal(raw, env, path) : null;
  }
  if (ctx.access.aud !== env.ACCESS_AUD) return null;
  const identity = await ctx.access.getIdentity();
  if (
    path === "/api/control" &&
    env.PUBLISHER_CLIENT_ID &&
    identity?.service_token_status === true &&
    identity.service_token_id === env.PUBLISHER_CLIENT_ID
  )
    return { email: env.OWNER_EMAIL };
  return identity?.email === env.OWNER_EMAIL ? { email: identity.email } : null;
}
