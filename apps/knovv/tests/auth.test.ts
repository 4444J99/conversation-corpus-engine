import { describe, it, expect } from "vitest";
import { authenticate } from "../server/worker";
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
