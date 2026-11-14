import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import { describe, it, expect } from "vitest";
import { authenticate, jwtPrincipal } from "../server/auth";
import type { Env } from "../server/api";
const env = {
  OWNER_EMAIL: "owner@example.test",
  ACCESS_AUD: "knovv-fixture-audience",
} as Env;
describe("worker identity boundary", () => {
  it("accepts only platform-verified owner identity for the pinned application", async () => {
    expect(
      await authenticate(env, {
        access: {
          aud: env.ACCESS_AUD,
          getIdentity: async () => ({ email: env.OWNER_EMAIL }),
        },
      } as any),
    ).toEqual({ email: env.OWNER_EMAIL });
    expect(await authenticate(env, {} as any)).toBeNull();
    expect(
      await authenticate(env, {
        access: {
          aud: "other",
          getIdentity: async () => ({ email: env.OWNER_EMAIL }),
        },
      } as any),
    ).toBeNull();
    expect(
      await authenticate(env, {
        access: {
          aud: env.ACCESS_AUD,
          getIdentity: async () => ({ email: "visitor@example.test" }),
        },
      } as any),
    ).toBeNull();
    expect(
      await authenticate({ ...env, ACCESS_AUD: "" }, {
        access: {
          aud: "",
          getIdentity: async () => ({ email: env.OWNER_EMAIL }),
        },
      } as any),
    ).toBeNull();
  });
});

it("restricts a pinned service identity to signed control requests", async () => {
  const machineEnv = { ...env, PUBLISHER_CLIENT_ID: "publisher-client" };
  const context = (identity: Record<string, unknown>) =>
    ({
      access: { aud: env.ACCESS_AUD, getIdentity: async () => identity },
    }) as any;
  const valid = context({
    service_token_status: true,
    service_token_id: "publisher-client",
  });
  expect(await authenticate(machineEnv, valid, "/api/control")).toEqual({
    email: env.OWNER_EMAIL,
  });
  for (const path of [
    "/",
    "/api/status",
    "/api/objects/a",
    "/api/control/extra",
  ])
    expect(await authenticate(machineEnv, valid, path)).toBeNull();
  for (const identity of [
    { common_name: "publisher-client" },
    { service_token_status: true, service_token_id: "other" },
    { service_token_status: false, service_token_id: "publisher-client" },
    { service_token_id: "publisher-client" },
  ])
    expect(
      await authenticate(machineEnv, context(identity), "/api/control"),
    ).toBeNull();
  expect(
    await authenticate(
      { ...machineEnv, PUBLISHER_CLIENT_ID: "" },
      valid,
      "/api/control",
    ),
  ).toBeNull();
});

it("verifies Access JWTs when the static-asset router omits ctx.access", async () => {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const jwk = {
    ...(await exportJWK(publicKey)),
    kid: "fixture-key",
    alg: "RS256",
    use: "sig",
  };
  const keys = createLocalJWKSet({ keys: [jwk] });
  const pin = {
    ...env,
    ACCESS_ISSUER: "https://team.cloudflareaccess.com",
    PUBLISHER_CLIENT_ID: "publisher-client",
  };
  const token /* allow-secret: synthetic JWT factory */ = (
    claims: Record<string, unknown>,
    issuer = pin.ACCESS_ISSUER,
    audience = pin.ACCESS_AUD,
  ) =>
    new SignJWT({ type: "app", ...claims })
      .setProtectedHeader({ alg: "RS256", kid: "fixture-key" })
      .setIssuer(issuer)
      .setAudience(audience)
      .setIssuedAt()
      .setExpirationTime("2m")
      .sign(privateKey);
  expect(
    await jwtPrincipal(await token({ email: env.OWNER_EMAIL }), pin, "/", keys),
  ).toEqual({ email: env.OWNER_EMAIL });
  const service = await token({ common_name: "publisher-client", sub: "" });
  expect(await jwtPrincipal(service, pin, "/api/control", keys)).toEqual({
    email: env.OWNER_EMAIL,
  });
  expect(await jwtPrincipal(service, pin, "/api/status", keys)).toBeNull();
  expect(
    await jwtPrincipal(
      await token({ email: "other@example.test" }),
      pin,
      "/",
      keys,
    ),
  ).toBeNull();
  for (const raw of [
    await token({ email: env.OWNER_EMAIL }, "https://attacker.example.test"),
    await token({ email: env.OWNER_EMAIL }, pin.ACCESS_ISSUER, "other"),
    service.slice(0, -5) + "wrong",
  ])
    await expect(
      jwtPrincipal(raw, pin, "/api/control", keys),
    ).rejects.toThrow();
});
