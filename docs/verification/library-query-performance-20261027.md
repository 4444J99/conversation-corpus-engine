# Library query performance (sanitized)

This page summarizes a query-performance investigation and remediation approach for repeated-passage summary queries. It omits private paths, production measurements, and receipt file names.

## Problem

A production-scale candidate exposed a costly repeated-passage summary query over large block/occurrence datasets. Profiling indicated that expensive table-row access patterns and repeated summary computation contributed to long runtimes, while a cheaper index-driven starting point was possible.

## Changes

- Reworked repeated-passage summary computation to group repeated occurrence keys before joining block metadata, reducing eligible block lookups.
- Added a derived covering index for new index builds (and drop/rebuild handling within savepoints) so the query planner can avoid fetching unnecessary block content.
- Reused completed indexing status in the builder to avoid redundant summary work.
- Added scoped progress/deadline handling so long-running SQL operations can be interrupted correctly while preserving nested handler behavior.

Existing schemas remain readable without migration; a previously published production generation is not retroactively modified by source changes alone.

## Validation

- Targeted store/build/resume/export tests and regression cases covered correctness (exact counts), query plans, existing-schema compatibility, cancellation/rollback behavior, and index rebuild interruption handling.
- Synthetic benchmark measurements demonstrated relative improvements in the test fixture, but those results do not predict production speedups.
- A bounded read-only trial of the rewritten query on a production candidate confirmed correct interruption at the time limit; production adoption of new derived indexes depends on rebuilding/indexing in that environment.

Aggregate query plans and benchmark details were retained in private evidence. This document contains no private source paths, record identifiers, or body content.
