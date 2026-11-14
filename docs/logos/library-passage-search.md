# Finding an evidence passage

A conversation can contain hundreds of useful paragraphs. Ranking its entire body
made a focused paragraph compete with the length of everything around it. The
passage index ranks the existing eligible prose blocks independently, while keeping
every source occurrence and every retained revision.

The retrieval unit is explicit. A passage is deduplicated text plus a selected source
occurrence; a conversation remains the broader search and counting unit. Automatic
selection prefers passage results within each query plan and preserves stronger
conversation matches before widening to an OR query. Handoffs name the immutable
revision, message and original character range. Canonical matching text never
substitutes for the source wording or its offsets.

The schema change is derived state. Published v1 stores remain readable; a copied
candidate gains the new index from blocks it already holds. Source fingerprints
remain unchanged, while publication identity includes the derived schema so the
upgrade can become current. Full verification checks the index, occurrences and
original message partitions together.

The documented synthetic acceptance library contains 660 conversations with more
than a 300-fold length range. Its benchmark follows real search handoffs and tests
the two-call and 2500-token retrieval budget. Installed private-library migration,
deployment and native-consumer acceptance remain separate operational steps.
