# KNOVV origin hints follow-up

Apply the merged origin-hint contract to Zettel quote derivation. Hints may
remove owner credit; a human hint never adds it. Preserve exact source text,
revision bindings, existing budgets, and the optional repeat census.

1. Add synthetic regression cases for agent, mixed, human and conflicting hints,
   with and without the repeat census, and observe failure before implementation.
2. Pass a validated OriginHints object through note export and quote derivation;
   expose --origin-hints for obsidian-md and refuse malformed files before writes.
3. Run the implicated attribution/Zettel tests and lint; publish a PR and submit
   its exact head once through merge-drain when eligible.
4. Keep private library data and operational receipts outside this public repo.

Runtime work: rebuild the installed CLI from merged main, export full-library
annotations with the private hints, and verify completion from its receipt.
Existing Cursor source verification/publication remains in domus-genoma-private.
