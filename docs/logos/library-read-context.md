# Library read context

The read interface must distinguish a missing publication from an unavailable
store and distinguish attachment observations from retained file bytes. Without
those distinctions, a consumer can mistake a disconnected drive for an empty
library or infer an attachment it cannot actually retrieve.

The integration of the unavailable-store and attachment-metadata branches retains
the current search planner and ranked-page implementation. Read exposes bounded
attachment metadata consistently for individual messages and ranges, while
withholding URL capabilities and invalid sizes. A normalized typed prefix is
reported only when valid; unknown boundaries and inline image-marker boundaries
remain explicitly unavailable. The immutable source is never rewritten to make
the read response look more complete.

This changes the repository implementation. Installing a new wheel into an
existing CLI/MCP environment, checking the real library, and observing native
consumers are separate acceptance steps. See the
[library interface contract](../knowledge-library.md#attachment-context-and-typed-text-boundaries).
