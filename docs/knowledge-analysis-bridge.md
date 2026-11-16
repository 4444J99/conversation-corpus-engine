# Native knowledge analysis bridge

The library can project a bounded selection of canonical revisions into the
existing LingFrame and atomic-substrata models. It reads the revision objects,
including alternate messages, rather than the condensed reading notes. Leading
prose, whitespace, code and repeated occurrences survive exactly. The outputs
are disposable analysis inputs; CCE remains the identity and revision authority.

```text
Canonical CCE revisions (read only)
  +-- selected messages and exact block spans
       +-- lingframe.json: Corpus -> Document -> message -> block
       +-- uaks.json: record -> message -> occurrence -> shared raw block
```

## Use

```sh
~/.local/bin/cce library analyze lattice --output /private/registered-store/derived/analysis/lattice-run --limit 5 --project-root /private/project --store-root /private/registered-store
```

The positional query and repeatable `--uid` selection are mutually exclusive.
Use `--target lingframe`, `--target uaks` or the default `both`. A query's receipt
states the total matched records, selected records and remaining records. A
record selection never silently drops an oversized source or clips its text.
Choose fewer records or explicitly adjust the limits when a budget is exceeded.

| Limit | Default | CLI option |
|---|---:|---|
| Selected records | 5; maximum 100 | `--limit` |
| Total decoded source bytes | 8 MiB | `--max-source-bytes` |
| Total output bytes, including receipt | 32 MiB | `--max-output-bytes` |
| Structural records, messages and block occurrences | 10,000 | `--max-atoms` |
| Elapsed processing time | 30 seconds | `--max-seconds` |

Output must be a new directory inside the registered private store, separate
from the input generation. Directories are created with mode `0700` and files
with mode `0600`. Existing outputs are never replaced. Byte and atom failures
occur before output creation; interruption during filesystem writes can leave
an incomplete directory, which is preserved. `receipt.json` is written last.
The CLI prints only the receipt: counts, revision identifiers, paths, hashes,
coverage and limits. Original text appears only in the private native files.

## Exact model mapping

| CCE element | LingFrame representation | UAKS representation |
|---|---|---|
| Selected identity | `Document.id` is the unchanged UID | Record node contains UID and revision metadata |
| Canonical revision | `Document.metadata.cce` contains version and digest | Record node `metadata.cce` contains version and digest |
| Main or alternate message | A theme container with role, source message ID and sequence | Message node plus `part_of` edge |
| Exact text span | Paragraph atom with the unchanged raw span | Content node addressed by full raw UTF-8 SHA256 |
| Source occurrence | Stable atom ID and metadata coordinates | An `occurrence` edge with stable ID and coordinates |
| Source provenance and coverage | Document metadata | Record-node metadata |

Theme and paragraph labels are the native structural containers. They do not
claim inferred themes or semantic paragraph classification: metadata identifies
each block as prose, code or separator. Every occurrence includes its UID,
revision digest, sequence (`main` or `alternate`), zero-based message and block
indices, source message ID, and Python codepoint start/end offsets. Concatenating
the blocks in order reconstructs the original normalized message exactly.
The canonical matching digest and its normalization steps are retained
separately from the raw-content digest. Only identical raw content shares a
UAKS content node; every source occurrence keeps its own edge.

Native models were checked at these immutable commits:

| Consumer | Commit | Loader |
|---|---|---|
| LingFrame | `9580334ec32a6cb175fc6c398975780e5b80e00f` | `framework.core.ontology.Corpus.from_dict` |
| atomic-substrata | `3e44b865bd9b144f6227dcb134030e1aa4c0af8e` | `scripts.graph_storage.GraphStorage` |

With each consumer's source available in its own Python environment:

```python
import json
from pathlib import Path
from framework.core.ontology import Corpus

corpus = Corpus.from_dict(json.loads(Path("RUN/lingframe.json").read_text()))
```

```python
from pathlib import Path
from scripts.graph_storage import GraphStorage

graph = GraphStorage(Path("RUN/uaks.json"))
occurrences = graph.find_by_relationship("occurrence")
```

The LingFrame projection always uses the multi-document envelope, even for one
record. Its pinned legacy `Corpus.to_dict()` special case flattens a single
document and loses document identity and metadata; preserve the emitted
envelope or serialize individual `Document.to_dict()` values when saving
analysis. The bridge does not invoke LingFrame's file atomizer, whose default
heading-driven mode omits leading prose. UAKS loads the completed graph once;
the bridge does not invoke its per-node whole-file save loop or create another
source intake database.

No consumer package, language model, embedding service or background job is
required by CCE. Actual native loader acceptance is distinct from semantic
analysis: `semantic_analysis_performed` is false in the receipt. Metadata-only
and partial records remain explicitly labeled. The current revision is selected;
previous revisions remain available through the canonical library history and
snapshot interfaces.

## Python API and validation

`library_analysis.export_analysis(library, destination, *, project_root,
store_root, query=None, uids=(), limit=5, target="both", limits=AnalysisLimits())`
requires a read-only `Library` and returns the same bounded receipt as the CLI.
Source object size, bounded decompression and SHA256 are checked before use.
A SQLite progress handler applies the deadline during the search, and remaining
processing checks it between records, atoms, serialization chunks and writes.

Regression tests exercise exact reconstruction, alternate messages, stable
identity across selections, shared raw blocks with distinct occurrence edges,
metadata-only retention, selection accounting, private output permissions,
overwrite/source-overlap refusal, budgets, deadlines and corrupted objects.
The pinned native classes were additionally loaded from Git-blob-verified plain
fixtures and checked for exact text reconstruction, unique atom IDs, document
metadata roundtrip, UAKS graph loading and source-edge traversal.
