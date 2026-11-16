import { KnowledgeDatabase } from '../src/database.js';
import { KnowledgeAtomizer } from '../src/atomizer.js';
import * as fs from 'fs';
import * as path from 'path';

function main() {
  const dir = 'intake/memory';
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.md'));
  const db = new KnowledgeDatabase('./db/knowledge.db');
  const atomizer = new KnowledgeAtomizer();
  
  let totalUnits = 0;
  for (const file of files) {
    const p = path.join(dir, file);
    const content = fs.readFileSync(p, 'utf8');
    const doc: any = {
      id: "memory_" + file,
      type: 'document',
      title: file,
      content,
      created: new Date(),
      modified: new Date(),
      tags: ['memory']
    };
    db.insertDocument(doc);
    const units = atomizer.atomize(doc);
    for (const unit of units) {
      unit.documentId = doc.id;
      db.insertAtomicUnit(unit);
    }
    totalUnits += units.length;
  }
  console.log(`Ingested ${files.length} files into ${totalUnits} units`);
}
main();
