#!/usr/bin/env node
import { readFileSync, writeFileSync } from 'fs';
import { resolve } from 'path';
import { fileURLToPath } from 'url';
import { WebClipperSource } from './sources/web-clipper.js';

/** Capture only an explicit newline-delimited URL list. Does not index reference-only URLs. */
export async function runClipQueue(args: string[]): Promise<number> {
  if (args.length !== 2) throw new Error('Usage: npx tsx src/clip-queue-cli.ts <urls.txt> <capture.json>');
  const [queuePath, outputPath] = args;
  if (resolve(queuePath) === resolve(outputPath)) throw new Error('Queue and output paths must differ');
  const urls = readFileSync(queuePath, 'utf8').split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#'));
  const result = await new WebClipperSource().exportAllWithReport({ urls });
  writeFileSync(outputPath, JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
  return result.report.status === 'complete' ? 0 : 2;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runClipQueue(process.argv.slice(2)).then(code => { process.exitCode = code; }).catch(() => {
    console.error('Queue capture failed. Usage: npx tsx src/clip-queue-cli.ts <urls.txt> <capture.json>');
    process.exitCode = 1;
  });
}
