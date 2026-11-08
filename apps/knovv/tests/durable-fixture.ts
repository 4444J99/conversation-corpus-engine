import { DurableObject } from "cloudflare:workers";
import { KnovvJournal, type JournalEnv } from "../server/durable";
import { SqliteDatabase, type SynchronousStatement } from "../server/database";

export { KnovvJournal };

// This probe is bundled only by durable.test.ts, never by the application Worker.
export class AdapterProbe extends DurableObject {
  async exercise() {
    const db = new SqliteDatabase(this.ctx.storage);
    this.ctx.storage.sql.exec(`
      CREATE TABLE item (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE audit (id INTEGER PRIMARY KEY, item INTEGER NOT NULL);
      CREATE TRIGGER audit_insert AFTER INSERT ON item BEGIN
        INSERT INTO audit(id,item) VALUES (NEW.id+1000,NEW.id);
      END;
    `);
    const insert = db.prepare("INSERT INTO item(id,value) VALUES (?,?)");
    const first = insert.bind(7, "first");
    const second = insert.bind(8, "second");
    const inserted = await first.run();
    const insertedSecond = await second.run();
    const skipped = await db
      .prepare("INSERT OR IGNORE INTO item(id,value) VALUES (?,?)")
      .bind(8, "ignored")
      .run();
    const selected = await db.prepare("SELECT * FROM item ORDER BY id").all();
    const one = await db.prepare("SELECT * FROM item ORDER BY id").first();
    const missing = await db.prepare("SELECT * FROM item WHERE id=99").first();
    let rolledBack = false;
    try {
      db.transactionSync((tx) => {
        tx.prepare("INSERT INTO item(id,value) VALUES (?,?)")
          .bind(9, "rollback")
          .run();
        tx.prepare("INSERT INTO item(id,value) VALUES (?,?)")
          .bind(7, "duplicate")
          .run();
      });
    } catch {
      rolledBack = true;
    }
    const committed = db.transactionSync((tx) => {
      const result = tx
        .prepare("INSERT INTO item(id,value) VALUES (?,?)")
        .bind(10, "committed")
        .run();
      return {
        result,
        row: tx.prepare("SELECT * FROM item WHERE id=10").first(),
      };
    });
    let asyncRefused = false;
    try {
      db.transactionSync(async (tx) => {
        tx.prepare("INSERT INTO item(id,value) VALUES (?,?)")
          .bind(11, "async rollback")
          .run();
        await Promise.resolve();
        tx.prepare("INSERT INTO item(id,value) VALUES (?,?)")
          .bind(12, "escaped write")
          .run();
      });
    } catch {
      asyncRefused = true;
    }
    let escaped: SynchronousStatement | undefined;
    db.transactionSync((tx) => {
      escaped = tx
        .prepare("INSERT INTO item(id,value) VALUES (?,?)")
        .bind(13, "expired transaction");
    });
    let escapeRefused = false;
    try {
      escaped!.run();
    } catch {
      escapeRefused = true;
    }
    await Promise.resolve();
    return {
      inserted,
      insertedSecond,
      skipped,
      selected,
      one,
      missing,
      rolledBack,
      committed,
      asyncRefused,
      escapeRefused,
      items: (await db.prepare("SELECT * FROM item ORDER BY id").all()).results,
      audit: (await db.prepare("SELECT * FROM audit ORDER BY id").all())
        .results,
    };
  }
}

interface FixtureEnv extends JournalEnv {
  JOURNAL: DurableObjectNamespace<KnovvJournal>;
  PROBE: DurableObjectNamespace<AdapterProbe>;
  TEST_OBJECT_NAME?: string;
}

// Auth is tested at the production Worker edge. This synthetic fixture exercises
// the trusted binding capability directly; it is not an alternate production entry.
export default {
  async fetch(request: Request, env: FixtureEnv): Promise<Response> {
    if (new URL(request.url).pathname === "/probe") {
      return Response.json(await env.PROBE.getByName("adapter").exercise());
    }
    const stub = env.JOURNAL.getByName(
      env.TEST_OBJECT_NAME ?? env.OWNER_PUBLIC_KEY,
    );
    if (new URL(request.url).pathname === "/fetch-is-not-exposed") {
      return stub.fetch(request);
    }
    return stub.serve(request);
  },
};
