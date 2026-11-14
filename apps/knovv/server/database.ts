type Row = Record<string, SqlStorageValue>;

export interface DatabaseResult<T> {
  success: boolean;
  results: T[];
  meta: { changes: number; last_row_id: number };
}

/** The journal's actual storage port, also satisfied by native D1 bindings. */
export interface Statement {
  bind(...values: SqlStorageValue[]): Statement;
  first<T extends Row = Row>(): Promise<T | null>;
  all<T extends Row = Row>(): Promise<DatabaseResult<T>>;
  run<T extends Row = Row>(): Promise<DatabaseResult<T>>;
}

export interface Database {
  prepare(query: string): Statement;
}

export interface SynchronousStatement {
  bind(...values: SqlStorageValue[]): SynchronousStatement;
  first<T extends Row = Row>(): T | null;
  all<T extends Row = Row>(): DatabaseResult<T>;
  run<T extends Row = Row>(): DatabaseResult<T>;
}

export interface SynchronousDatabase {
  prepare(query: string): SynchronousStatement;
}

class SqliteStatement implements SynchronousStatement {
  constructor(
    private readonly sql: SqlStorage,
    private readonly query: string,
    private readonly values: SqlStorageValue[] = [],
    private readonly assertActive: () => void = () => {},
  ) {}

  bind(...values: SqlStorageValue[]): SqliteStatement {
    return new SqliteStatement(this.sql, this.query, values, this.assertActive);
  }

  first<T extends Row = Row>(): T | null {
    this.assertActive();
    // No cursor crosses an await or escapes its transaction.
    return this.sql.exec<T>(this.query, ...this.values).toArray()[0] ?? null;
  }

  all<T extends Row = Row>(): DatabaseResult<T> {
    this.assertActive();
    const before = this.sql
      .exec<{ total: number }>("SELECT total_changes() AS total")
      .one().total;
    const results = this.sql.exec<T>(this.query, ...this.values).toArray();
    const counters = this.sql
      .exec<{ changes: number; last_row_id: number; total: number }>(
        "SELECT changes() AS changes, last_insert_rowid() AS last_row_id, total_changes() AS total",
      )
      .one();
    return {
      success: true,
      results,
      meta: {
        // rowsWritten includes trigger/index writes; changes() counts only the
        // direct statement. A read must not inherit a previous write's count.
        changes: counters.total === before ? 0 : counters.changes,
        last_row_id: counters.last_row_id,
      },
    };
  }

  run<T extends Row = Row>(): DatabaseResult<T> {
    return this.all<T>();
  }
}

class AsyncStatement implements Statement {
  constructor(private readonly statement: SqliteStatement) {}

  bind(...values: SqlStorageValue[]): AsyncStatement {
    return new AsyncStatement(this.statement.bind(...values));
  }

  async first<T extends Row = Row>(): Promise<T | null> {
    return this.statement.first<T>();
  }

  async all<T extends Row = Row>(): Promise<DatabaseResult<T>> {
    return this.statement.all<T>();
  }

  async run<T extends Row = Row>(): Promise<DatabaseResult<T>> {
    return this.statement.run<T>();
  }
}

export class SqliteDatabase implements Database {
  constructor(private readonly storage: DurableObjectStorage) {}

  prepare(query: string): Statement {
    return new AsyncStatement(new SqliteStatement(this.storage.sql, query));
  }

  /** Only synchronous statements belong here; exceptions roll back all writes. */
  transactionSync<T>(callback: (database: SynchronousDatabase) => T): T {
    return this.storage.transactionSync(() => {
      let active = true;
      const assertActive = () => {
        if (!active) throw new Error("transaction-closed");
      };
      try {
        const result = callback({
          prepare: (query) =>
            new SqliteStatement(this.storage.sql, query, [], assertActive),
        });
        if (result instanceof Promise) {
          // An erroneous async callback must neither commit its initial writes
          // nor retain a usable transaction after its first await.
          void result.catch(() => {});
          throw new TypeError("transaction-callback-must-be-synchronous");
        }
        return result;
      } finally {
        active = false;
      }
    });
  }
}
