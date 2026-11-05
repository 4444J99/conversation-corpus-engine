import * as fs from 'fs';
import * as path from 'path';
import { KnowledgeDatabase } from '../database.js';
import { AIFactory } from '../ai-factory.js';
import { AIProvider } from '../ai-types.js';
import { AtomicUnit } from '../types.js';
import { BatchProcessor } from '../batch-processor.js';

export interface WikiCompilerOptions {
  outputDir: string;
  /** Max parallel LLM workers (default: 1) */
  concurrency?: number;
  /** Delay between LLM requests in ms — controls rate (default: 500) */
  delayMs?: number;
  /** Cap total units processed in Phase A (default: all) */
  limit?: number;
  /** Max topics synthesized in Phase B (default: 25) */
  maxTopics?: number;
  /** Override the AI provider */
  provider?: AIProvider;
  // Legacy alias kept for back-compat
  batchConcurrency?: number;
}

export class WikiCompiler {
  private db: KnowledgeDatabase;
  private options: WikiCompilerOptions;
  private ai: AIProvider;
  private outputDir: string;
  /** Persisted between phases so runSynth() can use topics from runTopics() */
  private topicsMap: Record<string, AtomicUnit[]> = {};

  constructor(db: KnowledgeDatabase, options: WikiCompilerOptions) {
    this.db = db;
    this.options = options;
    this.outputDir = path.resolve(options.outputDir);
    this.ai = options.provider || AIFactory.getConfiguredProvider();
    if (!fs.existsSync(this.outputDir)) {
      fs.mkdirSync(this.outputDir, { recursive: true });
    }
  }

  // ── Convenience: run all phases ───────────────────────────────────────────

  public async compile(): Promise<void> {
    console.log(`Starting Wiki Compilation pipeline...`);
    console.log(`Output directory: ${this.outputDir}`);
    await this.runTopics();
    await this.runSynth();
    await this.runLink();
    console.log(`\nWiki compilation complete. Output saved to: ${this.outputDir}`);
  }

  // ── Phase A ───────────────────────────────────────────────────────────────

  public async runTopics(): Promise<void> {
    const concurrency = this.options.concurrency ?? this.options.batchConcurrency ?? 1;
    const delayMs = this.options.delayMs ?? 500;

    console.log(`Fetching all atomic units...`);
    let allUnits = this.db.getAllAtomicUnits().filter((u: AtomicUnit) => u.documentId && u.documentId.startsWith('memory_'));
    if (this.options.limit) {
      allUnits = allUnits.slice(0, this.options.limit);
    }
    console.log(`Processing ${allUnits.length} units  [concurrency=${concurrency}  delay=${delayMs}ms]`);

    const topicsMap: Record<string, AtomicUnit[]> = {};
    const processor = new BatchProcessor('.batch-checkpoints/wiki-topics', {
      concurrency,
      delayMs,
    });

    const topicPrompt = (unit: AtomicUnit) =>
      `Extract 1-3 topic labels from this text. Reply with ONLY a comma-separated list. No preamble, no explanation, no quotes.\n\nExample input: "How to set up Node.js authentication with JWT tokens"\nExample output: Node.js, Authentication, JWT\n\nText: ${unit.title} ${unit.content.slice(0, 500)}\n\nTopics:`;

    let results: Map<number, any>;
    if (processor.hasCheckpoint()) {
      const { results: initialResults } = processor.loadCheckpoint<AtomicUnit>();
      results = await processor.processWithState(allUnits, async (unit: AtomicUnit) => {
        const response = await this.ai.chat([{ role: 'user', content: topicPrompt(unit) }], { maxTokens: 60, temperature: 0.1 });
        return { unit, topics: this.parseTopicResponse(response) };
      }, initialResults);
    } else {
      results = await processor.process(allUnits, async (unit: AtomicUnit) => {
        const response = await this.ai.chat([{ role: 'user', content: topicPrompt(unit) }], { maxTokens: 60, temperature: 0.1 });
        return { unit, topics: this.parseTopicResponse(response) };
      });
    }

    for (const [_, res] of results) {
      if (res.success && res.result) {
        const { unit, topics } = res.result;
        for (const topic of topics) {
          const canonical = this.normalizeTopicName(topic);
          if (!topicsMap[canonical]) topicsMap[canonical] = [];
          topicsMap[canonical].push(unit);
        }
      }
    }

    const maxTopics = this.options.maxTopics ?? 25;
    const allTopics = Object.keys(topicsMap);
    console.log(`\nExtracted ${allTopics.length} unique topics. Keeping top ${maxTopics}...`);

    const topTopics = allTopics
      .sort((a, b) => topicsMap[b].length - topicsMap[a].length)
      .slice(0, maxTopics);

    for (const topic of topTopics) {
      console.log(`  → ${topic} (${topicsMap[topic].length} units)`);
    }

    this.topicsMap = {};
    for (const topic of topTopics) {
      this.topicsMap[topic] = topicsMap[topic];
    }

    // Persist topics map to disk so runSynth() can work standalone
    const topicsFile = path.join(this.outputDir, '.topics.json');
    fs.writeFileSync(topicsFile, JSON.stringify(this.topicsMap, null, 2));
    console.log(`\nTopics saved to ${topicsFile}`);
  }

  // ── Phase B ───────────────────────────────────────────────────────────────

  public async runSynth(): Promise<void> {
    const concurrency = this.options.concurrency ?? this.options.batchConcurrency ?? 1;
    const delayMs = this.options.delayMs ?? 500;

    // Load topics from disk if not already in memory (e.g. standalone invocation)
    if (Object.keys(this.topicsMap).length === 0) {
      const topicsFile = path.join(this.outputDir, '.topics.json');
      if (!fs.existsSync(topicsFile)) {
        throw new Error(`No topics found. Run 'topics' phase first. Expected: ${topicsFile}`);
      }
      this.topicsMap = JSON.parse(fs.readFileSync(topicsFile, 'utf-8'));
      console.log(`Loaded ${Object.keys(this.topicsMap).length} topics from ${topicsFile}`);
    }

    const topics = Object.keys(this.topicsMap);
    console.log(`Synthesizing ${topics.length} wiki pages  [concurrency=${concurrency}  delay=${delayMs}ms]`);

    // Write index page
    let indexContent = `# Knowledge Wiki Index\n\n`;
    for (const topic of [...topics].sort()) {
      indexContent += `- [${topic}](./${this.slugify(topic)}.md) (${this.topicsMap[topic].length} units)\n`;
    }
    fs.writeFileSync(path.join(this.outputDir, 'index.md'), indexContent);

    const processor = new BatchProcessor('.batch-checkpoints/wiki-pages', {
      concurrency,
      delayMs,
    });

    await processor.process(topics, async (topic) => {
      const slug = this.slugify(topic);
      const units = this.topicsMap[topic];
      const selectedUnits = units.slice(0, 15);
      const unitsText = selectedUnits
        .map((u, i) => `--- Unit ${i + 1} ---\nTitle: ${u.title}\n${u.content}`)
        .join('\n\n');

      const prompt = `Write a wiki article about "${topic}" using ONLY the source material below. Do not invent facts. Synthesize the information into a structured markdown document with headings and paragraphs. Stay faithful to the source material.\n\nSource Material:\n${unitsText}\n\nWrite the article now:`;

      const response = await this.ai.chat(
        [{ role: 'user', content: prompt }],
        { maxTokens: 4000, temperature: 0.3 }
      );

      fs.writeFileSync(path.join(this.outputDir, `${slug}.md`), `# ${topic}\n\n${response}`);
      return slug;
    });
  }

  // ── Phase C ───────────────────────────────────────────────────────────────

  public async runLink(): Promise<void> {
    // Load topics if needed
    if (Object.keys(this.topicsMap).length === 0) {
      const topicsFile = path.join(this.outputDir, '.topics.json');
      if (fs.existsSync(topicsFile)) {
        this.topicsMap = JSON.parse(fs.readFileSync(topicsFile, 'utf-8'));
      }
    }

    const topics = Object.keys(this.topicsMap);
    if (topics.length === 0) {
      console.log('No topics to cross-link — skipping.');
      return;
    }

    console.log(`Cross-linking ${topics.length} topics across wiki pages...`);
    const sortedTopics = [...topics].sort((a, b) => b.length - a.length);

    let linked = 0;
    for (const file of fs.readdirSync(this.outputDir)) {
      if (!file.endsWith('.md') || file === 'index.md') continue;
      const filePath = path.join(this.outputDir, file);
      let content = fs.readFileSync(filePath, 'utf-8');
      let modified = false;
      const currentSlug = file.replace('.md', '');

      for (const topic of sortedTopics) {
        const slug = this.slugify(topic);
        if (slug === currentSlug) continue;
        const regex = new RegExp(`(?<!\\[)(?:\\b)(${this.escapeRegExp(topic)})(?:\\b)(?!\\]|\\()`, 'gi');
        if (regex.test(content)) {
          content = content.replace(regex, `[$1](./${slug}.md)`);
          modified = true;
        }
      }

      if (modified) {
        fs.writeFileSync(filePath, content);
        linked++;
      }
    }
    console.log(`Cross-linked ${linked} pages.`);
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  private parseTopicResponse(response: string): string[] {
    let text = response.trim();
    const preamblePatterns = [
      /^.*?(?:topics?|entities?|labels?)\s*(?:are|is|:)\s*/is,
      /^(?:I would identify|Based on|Here are|The following).*?:\s*/is,
      /^.*?comma-separated list.*?:\s*/is,
    ];
    for (const pattern of preamblePatterns) {
      text = text.replace(pattern, '');
    }
    const lines = text.split('\n').map(l => l.trim()).filter(l => l.length > 0);
    const candidateLine = lines[0] || text;
    return candidateLine
      .split(',')
      .map(t => t.trim())
      .map(t => t.replace(/^["'\-*•\d.]+\s*/, ''))
      .map(t => t.replace(/["']+$/g, ''))
      .filter(t => t.length > 1 && t.length < 60)
      .slice(0, 3);
  }

  private normalizeTopicName(topic: string): string {
    return topic.trim().replace(/^["']|["']$/g, '');
  }

  private slugify(text: string): string {
    return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)+/g, '');
  }

  private escapeRegExp(string: string): string {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }
}



