import { encoder, sha256 } from "./crypto";
export type Postings = Record<string, number[]>;
export const terms = (query: string) =>
  [...new Set(query.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [])].slice(0, 16);
export async function searchIndex(
  query: string,
  load: (bucket: string) => Promise<Postings>,
): Promise<{
  ordinals: number[];
  total: number;
  plan: "and" | "or" | "empty";
  truncated: boolean;
}> {
  const tokens = terms(query);
  if (!tokens.length)
    return { ordinals: [], total: 0, plan: "empty", truncated: false };
  const cache = new Map<string, Promise<Postings>>();
  const lists = await Promise.all(
    tokens.map(async (token) => {
      const bucket = sha256(encoder.encode(token)).slice(0, 2);
      if (!cache.has(bucket)) cache.set(bucket, load(bucket));
      const index = await cache.get(bucket)!;
      if (!Object.hasOwn(index, token)) return [];
      const list = index[token];
      if (
        !Array.isArray(list) ||
        list.some((n) => !Number.isSafeInteger(n) || n < 0)
      )
        throw new Error("invalid-search-index");
      return list;
    }),
  );
  const hits = new Map<number, number>();
  for (const list of lists)
    for (const ordinal of list) hits.set(ordinal, (hits.get(ordinal) ?? 0) + 1);
  let ranked = [...hits.entries()].filter(
      ([, count]) => count === tokens.length,
    ),
    plan: "and" | "or" = "and";
  if (!ranked.length) {
    ranked = [...hits.entries()];
    plan = "or";
  }
  ranked.sort((a, b) => b[1] - a[1] || a[0] - b[0]);
  return {
    ordinals: ranked.slice(0, 1000).map(([id]) => id),
    total: ranked.length,
    plan,
    truncated: tokens.length === 16 || ranked.length > 1000,
  };
}
