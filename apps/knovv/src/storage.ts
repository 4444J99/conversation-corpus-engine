export interface StoragePort {
  get<T>(key: string): Promise<T | undefined>;
  set(key: string, value: unknown): Promise<void>;
  remove(key: string): Promise<void>;
  removeDraft(key: string, revisionId: string): Promise<void>;
  keys(prefix: string): Promise<string[]>;
  putCipher(id: string, bytes: Uint8Array): Promise<void>;
}
const CACHE_BYTES = 32 * 1024 * 1024;
type CacheEntry = { id: string; size: number };
function cacheOrder(order: CacheEntry[], id: string, size: number) {
  const next = order.filter((entry) => entry.id !== id);
  next.push({ id, size });
  let total = next.reduce((sum, entry) => sum + entry.size, 0);
  const removed: string[] = [];
  while (total > CACHE_BYTES) {
    const item = next.shift()!;
    total -= item.size;
    removed.push(item.id);
  }
  return { next, removed };
}
export class MemoryStorage implements StoragePort {
  values = new Map<string, unknown>();
  async get<T>(key: string) {
    return structuredClone(this.values.get(key)) as T | undefined;
  }
  async set(key: string, value: unknown) {
    this.values.set(key, structuredClone(value));
  }
  async remove(key: string) {
    this.values.delete(key);
  }
  async removeDraft(key: string, revisionId: string) {
    if (
      (this.values.get(key) as { envelope?: { id?: string } } | undefined)
        ?.envelope?.id === revisionId
    )
      this.values.delete(key);
  }
  async keys(prefix: string) {
    return [...this.values.keys()].filter((k) => k.startsWith(prefix));
  }
  async putCipher(id: string, bytes: Uint8Array) {
    if (bytes.length > CACHE_BYTES) return;
    const { next, removed } = cacheOrder(
      (this.values.get("cache-order") as CacheEntry[]) ?? [],
      id,
      bytes.length,
    );
    for (const old of removed) this.values.delete("object:" + old);
    this.values.set("object:" + id, structuredClone(bytes));
    this.values.set("cache-order", next);
  }
}
export class BrowserStorage implements StoragePort {
  private database: Promise<IDBDatabase>;
  constructor() {
    this.database = new Promise((resolve, reject) => {
      const r = indexedDB.open("knovv-encrypted-v1", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("ciphertext");
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(new Error("device-storage-unavailable"));
    });
  }
  private async run<T>(
    mode: IDBTransactionMode,
    operation: (store: IDBObjectStore) => IDBRequest<T>,
  ): Promise<T> {
    const db = await this.database;
    return new Promise((resolve, reject) => {
      const transaction = db.transaction("ciphertext", mode),
        request = operation(transaction.objectStore("ciphertext"));
      let value: T;
      request.onsuccess = () => {
        value = request.result;
      };
      transaction.oncomplete = () => resolve(value);
      transaction.onerror = () =>
        reject(new Error("device-storage-unavailable"));
      transaction.onabort = () =>
        reject(new Error("device-storage-unavailable"));
    });
  }
  async get<T>(key: string) {
    return this.run<T | undefined>("readonly", (s) => s.get(key));
  }
  async set(key: string, value: unknown) {
    await this.run("readwrite", (s) => s.put(value, key));
  }
  async remove(key: string) {
    await this.run("readwrite", (s) => s.delete(key));
  }
  async removeDraft(key: string, revisionId: string) {
    const db = await this.database;
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction("ciphertext", "readwrite"),
        store = tx.objectStore("ciphertext"),
        get = store.get(key);
      get.onsuccess = () => {
        if (get.result?.envelope?.id === revisionId) store.delete(key);
      };
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(new Error("device-storage-unavailable"));
      tx.onabort = () => reject(new Error("device-storage-unavailable"));
    });
  }
  async keys(prefix: string) {
    return (
      await this.run<IDBValidKey[]>("readonly", (s) => s.getAllKeys())
    ).filter((k): k is string => typeof k === "string" && k.startsWith(prefix));
  }
  async putCipher(id: string, bytes: Uint8Array) {
    if (bytes.length > CACHE_BYTES) return;
    const db = await this.database;
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction("ciphertext", "readwrite"),
        store = tx.objectStore("ciphertext"),
        get = store.get("cache-order");
      get.onsuccess = () => {
        const { next, removed } = cacheOrder(
          get.result ?? [],
          id,
          bytes.length,
        );
        for (const old of removed) store.delete("object:" + old);
        store.put(bytes, "object:" + id);
        store.put(next, "cache-order");
      };
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(new Error("device-storage-unavailable"));
      tx.onabort = () => reject(new Error("device-storage-unavailable"));
    });
  }
}
