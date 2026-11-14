import { encoder, sha256 } from "./crypto";
import { ByteCache } from "./byte-cache";
type Compact = { count: number; delta?: string; ranges?: string };
export type Postings = Record<string, number[] | Compact>;
type Range = [number, number];
interface List {
  count: number;
  bytes: number;
  ranges: () => Generator<Range>;
}
export const terms = (query: string) =>
  [...new Set(query.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [])].slice(0, 16);
function invalid(): never {
  throw new Error("invalid-search-index");
}
function* coalesce(numbers: Iterable<number>): Generator<Range> {
  let start = -1,
    end = -1;
  for (const n of numbers) {
    if (start < 0) {
      start = end = n;
      continue;
    }
    if (n === end + 1) {
      end = n;
      continue;
    }
    yield [start, end];
    start = end = n;
  }
  if (start >= 0) yield [start, end];
}
function* varints(raw: Uint8Array) {
  let value = 0,
    shift = 0;
  for (const byte of raw) {
    if (shift > 28) invalid();
    value += (byte & 127) * 2 ** shift;
    if (byte & 128) {
      shift += 7;
      continue;
    }
    if (shift > 0 && byte === 0) invalid();
    yield value;
    value = shift = 0;
  }
  if (shift || value) invalid();
}
function posting(value: number[] | Compact): List {
  if (Array.isArray(value)) {
    let previous = -1;
    for (const n of value) {
      if (!Number.isSafeInteger(n) || n <= previous || n > 0xffffffff)
        invalid();
      previous = n;
    }
    return {
      count: value.length,
      bytes: value.length * 8,
      ranges: () => coalesce(value),
    };
  }
  if (
    !value ||
    !Number.isSafeInteger(value.count) ||
    value.count < 0 ||
    value.count > 0x100000000
  )
    invalid();
  const isRanges = typeof value.ranges === "string";
  const data = isRanges ? value.ranges : value.delta;
  if (
    typeof data !== "string" ||
    (isRanges && value.delta !== undefined) ||
    data.length > 48 * 1024 * 1024
  )
    invalid();
  let binary: string;
  try {
    binary = atob(data);
  } catch {
    invalid();
  }
  const raw = new Uint8Array(binary.length);
  for (let i = 0; i < raw.length; i++) raw[i] = binary.charCodeAt(i);
  const count = value.count;
  function* ranges(): Generator<Range> {
    let previous = -1,
      seen = 0;
    if (isRanges) {
      const numbers = varints(raw);
      for (const gap of numbers) {
        const next = numbers.next();
        if (next.done || gap < 1 || next.value < 1) invalid();
        const start = previous + gap,
          end = start + next.value - 1;
        if (start <= previous || end > 0xffffffff) invalid();
        seen += end - start + 1;
        previous = end;
        yield [start, end];
      }
    } else {
      function* numbers() {
        for (const delta of varints(raw)) {
          if (delta < 1 || previous + delta > 0xffffffff) invalid();
          previous += delta;
          seen++;
          yield previous;
        }
      }
      yield* coalesce(numbers());
    }
    if (seen !== count) invalid();
  }
  for (const _ of ranges()) {
    /* validate the full compact stream before trusting its count */
  }
  return { count, bytes: raw.length, ranges };
}
const empty: List = { count: 0, bytes: 0, ranges: function* () {} };
function* intersect(
  a: Generator<Range>,
  b: Generator<Range>,
): Generator<Range> {
  let left = a.next(),
    right = b.next();
  while (!left.done && !right.done) {
    const start = Math.max(left.value[0], right.value[0]),
      end = Math.min(left.value[1], right.value[1]);
    if (start <= end) yield [start, end];
    if (left.value[1] <= right.value[1]) left = a.next();
    else right = b.next();
  }
}
function addFirst(target: number[], start: number, end: number) {
  for (let n = start; n <= end && target.length < 1000; n++) target.push(n);
}
function intersection(lists: List[]) {
  let factory = lists[0].ranges;
  for (const list of lists.slice(1)) {
    const previous = factory;
    factory = () => intersect(previous(), list.ranges());
  }
  const ordinals: number[] = [];
  let total = 0;
  for (const [start, end] of factory()) {
    total += end - start + 1;
    addFirst(ordinals, start, end);
  }
  return { ordinals, total };
}
function union(lists: List[]) {
  const states = lists.map((list) => {
    const stream = list.ranges(),
      first = stream.next();
    return {
      stream,
      range: first.done ? null : first.value,
      point: first.done ? Infinity : first.value[0],
      entering: true,
    };
  });
  const ranks: number[][] = Array.from({ length: lists.length + 1 }, () => []);
  let active = 0,
    total = 0,
    previous = 0;
  while (true) {
    let point = Infinity;
    for (const state of states) point = Math.min(point, state.point);
    if (point === Infinity) break;
    if (active && point > previous) {
      total += point - previous;
      addFirst(ranks[active], previous, point - 1);
    }
    for (const state of states) {
      while (state.point === point) {
        if (state.entering) {
          active++;
          state.point = state.range![1] + 1;
          state.entering = false;
        } else {
          active--;
          const next = state.stream.next();
          state.range = next.done ? null : next.value;
          state.point = next.done ? Infinity : next.value[0];
          state.entering = true;
        }
      }
    }
    previous = point;
  }
  const ordinals: number[] = [];
  for (let score = lists.length; score > 0 && ordinals.length < 1000; score--)
    ordinals.push(...ranks[score].slice(0, 1000 - ordinals.length));
  return { ordinals, total };
}
export async function searchIndex(
  query: string,
  load: (bucket: string) => Promise<Postings>,
  prefix = 2,
) {
  const tokens = terms(query);
  if (!tokens.length)
    return { ordinals: [], total: 0, plan: "empty" as const, truncated: false };
  const cache = new ByteCache<string, Postings>(8 * 1024 * 1024),
    lists: List[] = [];
  let bytes = 0;
  for (const token of tokens) {
    const bucket = sha256(encoder.encode(token)).slice(0, prefix);
    let index = cache.get(bucket);
    if (!index) {
      index = await load(bucket);
      cache.set(bucket, index);
    }
    const list = Object.hasOwn(index, token) ? posting(index[token]) : empty;
    bytes += list.bytes;
    if (bytes > 32 * 1024 * 1024) throw new Error("search-query-memory-bound");
    lists.push(list);
  }
  const all = intersection(lists),
    result = all.total ? all : union(lists);
  return {
    ...result,
    plan: all.total ? ("and" as const) : ("or" as const),
    truncated: tokens.length === 16 || result.total > 1000,
  };
}
