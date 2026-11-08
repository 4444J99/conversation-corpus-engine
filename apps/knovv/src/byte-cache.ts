import { encoder } from "./crypto";
/** Payload-byte budget, independent of persistent ciphertext and trust checkpoints. */
export class ByteCache<K, V> extends Map<K, V> {
  bytes = 0;
  private sizes = new Map<K, number>();
  constructor(readonly budget: number) {
    super();
  }
  override set(key: K, value: V) {
    this.delete(key);
    const size = encoder.encode(JSON.stringify(value)).length;
    if (size > this.budget) return this;
    while (this.bytes + size > this.budget && this.size)
      this.delete(this.keys().next().value!);
    super.set(key, value);
    this.sizes.set(key, size);
    this.bytes += size;
    return this;
  }
  override get(key: K) {
    const value = super.get(key);
    if (value !== undefined) {
      super.delete(key);
      super.set(key, value);
    }
    return value;
  }
  override delete(key: K) {
    const removed = super.delete(key);
    if (removed) {
      this.bytes -= this.sizes.get(key) ?? 0;
      this.sizes.delete(key);
    }
    return removed;
  }
  override clear() {
    super.clear();
    this.sizes.clear();
    this.bytes = 0;
  }
}
