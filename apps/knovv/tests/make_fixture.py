"""Create an entirely synthetic local browser fixture; never use real credentials."""
import json
import os
import sys
from pathlib import Path

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.archive_sealed_credentials import protect_identity
from conversation_corpus_engine.archive_sealed_crypto import Identity, canonical, digest, sign
from conversation_corpus_engine.knovv_projection import export_bundle
from conversation_corpus_engine.library_store import Library

root = Path(sys.argv[1])
root.mkdir(mode=0o700, parents=True, exist_ok=True)
wiki = root / "wiki"
wiki.mkdir(mode=0o700)
(wiki / "Welcome.md").write_text("---\ntitle: A place for understanding\ntype: wiki\nauthor: fixture\n---\n# Start with the source\n\nThis is a synthetic local workspace for verifying KNOVV.\n\n## Follow the evidence\n\nRead [[Lattice]] and the [[Synthetic project]]. Search for **lattice sources** to locate a source passage.\n\n## Leave something useful\n\nCapture a thought, edit this page, then reopen it.\n")
(wiki / "Lattice.md").write_text("---\ntitle: Lattice\ntype: zettel\nauthor: fixture\n---\n# Lattice\n\nA small, linked idea can preserve its origins.\n\nReturn to [[Welcome]].\n")
(wiki / "Synthetic project.md").write_text("---\ntitle: Synthetic project\ntype: project\nauthor: fixture\n---\n# Synthetic project\n\n## Discussion\n\nA presentation should become a tool.\n\n## Decision\n\nKeep knowledge private and make it usable.\n\n## Implementation\n\nThis fixture exercises the browser workflow, not real-data acceptance.\n\n## Verification\n\nOpen [[Welcome]], edit it, and check history.\n")
owner = Identity("knovv", "synthetic-publisher", os.urandom(32), os.urandom(32))
device = Identity("knovv", "synthetic-browser", os.urandom(32), os.urandom(32))
with Library.create(root / "library.sqlite") as library:
    library.put(Conversation(provider="chatgpt", source_id="fixture", title="Lattice inquiry", messages=[Message(role="user", text="A lattice remembers its sources. Every knowledge claim should preserve the passage that supports it.", id="m1", channel="typed"), Message(role="assistant", text="A derived explanation should remain connected to the original material.", id="m2", channel="reply")], provenance={"source_file":"synthetic.json", "source_sha256":"a"*64}))
    export_bundle(library, wiki, root / "bundle", owner, [owner.recipient(), device.recipient()])
head = json.loads((root / "bundle" / "HEAD.knovv").read_bytes())
grant = sign({"schema":"knovv-device.v1", "id":digest(device.signing_public().encode()), "signing_public":device.signing_public(), "generation":1, "revoked":False},owner)
pack = {"schema":"knovv-device-package.v1", "credential":json.loads(protect_identity(device,b"fixture-only-passphrase")), "trust":{"owner":owner.signing_public(),"vault":head["vault"],"generation":head["generation"],"head":digest(canonical(head))}}
for name,value in (("device.json",pack),("grant.json",grant)):
    path=root/name
    path.write_bytes(canonical(value))
    path.chmod(0o600)
print(json.dumps({"root":str(root),"owner":owner.signing_public()}))
