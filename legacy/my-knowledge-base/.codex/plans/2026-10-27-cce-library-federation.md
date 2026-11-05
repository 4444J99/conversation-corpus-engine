# CCE library federation

Use the existing federation cache to consume CCE published generations. CCE owns stable identity, source revisions and provenance; no parallel ingestion ledger is created.

1. Validate the atomic publication pointer, report, index, inventory and note hashes.
2. Stream bounded notes into existing federated_documents; preserve UID and revision metadata.
3. Retain cached identities absent from later CCE generations and roll back failed scans.
4. Verify synthetic publication, malformed data, replay, budgets and special-file handling; read a real CCE export before acceptance.

Source scope: provider, federation indexer, focused tests, operations documentation. No private corpus material belongs in this repository.
