/**
 * Wiki Compiler CLI — modular, per-phase, rate-limited
 *
 * Usage:
 *   npm run compile:wiki [phase] [options]
 *
 * Phases:
 *   topics   — Phase A: extract topics from atomic units (LLM-heavy)
 *   synth    — Phase B: synthesize wiki pages per topic (LLM-heavy)
 *   link     — Phase C: cross-link pages (pure text, no LLM)
 *   all      — Run all phases in sequence (default)
 *
 * Options:
 *   --rps <n>          Max LLM requests per second (default: 2)
 *   --concurrency <n>  Parallel workers (default: 1)
 *   --limit <n>        Max units/topics to process (default: all)
 *   --output <dir>     Output directory (default: ./compiled-wiki)
 *   --dry-run          Show what would run without calling the LLM
 *
 * Examples:
 *   npm run compile:wiki topics --rps 1
 *   npm run compile:wiki synth --rps 2 --concurrency 2
 *   npm run compile:wiki link
 *   npm run compile:wiki all --rps 1
 */

import { config } from 'dotenv';
config();

import { KnowledgeDatabase } from '../database.js';
import { WikiCompiler } from './wiki-compiler.js';

// ── Argument parsing ──────────────────────────────────────────────────────────

function parseArgs(argv: string[]): {
  phase: string;
  rps: number;
  concurrency: number;
  limit: number | undefined;
  maxTopics: number;
  outputDir: string;
  dryRun: boolean;
} {
  const args = argv.slice(2);

  const phase = ['topics', 'synth', 'link', 'all'].includes(args[0]) ? args[0] : 'all';

  const get = (flag: string) => {
    const i = args.indexOf(flag);
    return i !== -1 ? args[i + 1] : undefined;
  };

  const rps = parseFloat(get('--rps') ?? '2');
  const concurrency = parseInt(get('--concurrency') ?? '1', 10);
  const limitRaw = get('--limit');
  const limit = limitRaw ? parseInt(limitRaw, 10) : undefined;
  const maxTopics = parseInt(get('--maxTopics') ?? '50', 10);
  const outputDir = get('--output') ?? process.env.WIKI_OUTPUT ?? './compiled-wiki';
  const dryRun = args.includes('--dry-run');

  return { phase, rps, concurrency, limit, maxTopics, outputDir, dryRun };
}

// ── Entrypoint ────────────────────────────────────────────────────────────────

async function main() {
  const { phase, rps, concurrency, limit, maxTopics, outputDir, dryRun } = parseArgs(process.argv);
  const dbPath = process.env.DB_PATH ?? './db/knowledge.db';

  // Derive delayMs from rps: spread requests so we don't exceed the rate limit.
  // With concurrency=1 this is simple: delayMs = 1000/rps.
  // With concurrency>1 we multiply to avoid bursting.
  const delayMs = Math.round((1000 / rps) * concurrency);

  console.log('┌─────────────────────────────────────────┐');
  console.log('│         Wiki Compiler — Modular CLI      │');
  console.log('└─────────────────────────────────────────┘');
  console.log(`  Phase       : ${phase}`);
  console.log(`  Rate limit  : ${rps} req/s  (delay: ${delayMs}ms)`);
  console.log(`  Concurrency : ${concurrency}`);
  console.log(`  Limit       : ${limit ?? 'all'}`);
  console.log(`  Max topics  : ${maxTopics}`);
  console.log(`  Output      : ${outputDir}`);
  console.log(`  Dry run     : ${dryRun}`);
  console.log(`  DB          : ${dbPath}`);
  console.log('');

  if (dryRun) {
    console.log('🔍 Dry-run mode — no LLM calls will be made. Exiting.');
    process.exit(0);
  }

  const db = new KnowledgeDatabase(dbPath);

  try {
    const compiler = new WikiCompiler(db, {
      outputDir,
      concurrency,
      delayMs,
      limit,
      maxTopics,
    });

    if (phase === 'topics' || phase === 'all') {
      console.log('\n── Phase A: Topic Extraction ──────────────────');
      await compiler.runTopics();
    }

    if (phase === 'synth' || phase === 'all') {
      console.log('\n── Phase B: Page Synthesis ────────────────────');
      await compiler.runSynth();
    }

    if (phase === 'link' || phase === 'all') {
      console.log('\n── Phase C: Cross-linking ─────────────────────');
      await compiler.runLink();
    }

    console.log('\n✅ Done.');
  } catch (err) {
    console.error('\n❌ Error:', err);
    process.exit(1);
  } finally {
    db.close();
  }
}

main().catch(console.error);

