# The gap between an archive and the apps that feed it

An archive of provider conversations ends on some date. The desktop apps keep going: they hold
recent conversations in local caches that the export a person last requested does not contain.
`cce app-cache gap` answers one question about each provider, using no message text and reporting none:
what do the staged caches know that the archive does not, and for that part, can it be ingested
now or does the person have to request the provider's export.

The comparison uses ids and times only. A conversation the archive does not hold is `new`. One it
holds, but that the app stamped after the archive's last activity, is `newer`. Both are split by
whether a full body was staged and whether that body is itself newer than the archive's end, since
a stale body under fresh list metadata cannot fill a gap. The app's time is its own stamp, not
proof of new messages, so `newer` counts are an upper bound and the report says so.

Three guarantees hold the report to what it claims. Message text is discarded while a staged file
is parsed and never returned or written. The archive is read as a stable snapshot: an ingest
replaces shards one file at a time, so a read whose shard names, sizes and times differ before and
after is repeated, and refused after a few tries rather than reported as a mix of two generations.
The report directory is written aside and moved into place in one rename, so a failed run leaves
nothing behind to block a retry. The per-conversation list carries titles and stays in the private
output directory; stdout and `coverage.json` carry counts and dates.
