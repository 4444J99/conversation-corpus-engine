import { DurableObject } from "cloudflare:workers";
import { api, json, type Env } from "./api";
import { SqliteDatabase } from "./database";
import schema from "./schema.sql";

export type JournalEnv = Omit<Env, "DB">;

/** One journal per owner public key, reachable only through its Worker binding. */
export class KnovvJournal extends DurableObject<JournalEnv> {
  #database: SqliteDatabase;
  #trusted = false;

  constructor(ctx: DurableObjectState, env: JournalEnv) {
    super(ctx, env);
    this.#database = new SqliteDatabase(ctx.storage);
    ctx.blockConcurrencyWhile(async () => {
      if (!/^[0-9a-f]{64}$/.test(env.OWNER_PUBLIC_KEY) || !env.OWNER_EMAIL)
        return;
      // The sole initialization gate contains only synchronous local storage.
      ctx.storage.transactionSync(() => {
        ctx.storage.sql.exec(schema);
        const identity = ctx.storage.sql
          .exec<{ owner: string }>(
            "SELECT owner FROM knovv_identity WHERE id=1",
          )
          .toArray()[0];
        if (identity && identity.owner !== env.OWNER_PUBLIC_KEY) return;
        ctx.storage.sql.exec(
          "INSERT OR IGNORE INTO knovv_identity(id,owner) VALUES (1,?)",
          env.OWNER_PUBLIC_KEY,
        );
        this.#trusted = true;
      });
    });
  }

  // The Worker authenticates Access before invoking this internal capability.
  // No fetch handler or independent public authentication bypass is exported.
  async serve(request: Request): Promise<Response> {
    if (!this.#trusted) return json({ error: "trust-not-configured" }, 503);
    return api(
      request,
      { ...this.env, DB: this.#database },
      { email: this.env.OWNER_EMAIL },
    );
  }
}
