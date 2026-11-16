# CCE H1 Launch Posts

## Hacker News

**Title:** Show HN: Search all your AI conversations from one place

**Post:**

I built Conversation Corpus Engine, a Python CLI for turning exported AI chats into a searchable, governed corpus.

It currently handles multi-provider conversation archives, source freshness, corpus validation, federation, grounded retrieval, promotion policy replay, candidate diffing, and JSON schema contracts. The new MCP server exposes the same retrieval layer through stdio, so a tool host can query a local corpus and receive cited answers instead of loose keyword matches.

The core use case is simple: your useful AI conversations are spread across ChatGPT, Claude, Gemini, Perplexity, Copilot, Grok, DeepSeek, and Mistral. CCE makes them inspectable as one corpus, with enough governance to know whether a source is fresh, validated, and safe to promote.

Install:

```bash
pipx install "conversation-corpus-engine[mcp]"
cce-mcp --project-root /path/to/project
```

Repo: https://github.com/organvm-i-theoria/conversation-corpus-engine

I am especially looking for feedback from people who treat AI conversations as work product: researchers, engineers, consultants, and teams trying to preserve decision history across tools.

## Social Short

I shipped a first public surface for Conversation Corpus Engine: a Python CLI and stdio MCP server for searching AI conversation archives across providers with citations, validation, source freshness, and federation.

Install:

```bash
pipx install "conversation-corpus-engine[mcp]"
```

## Social Thread

1. AI conversations are becoming work product, but they are scattered across providers and hard to search after the fact.

2. Conversation Corpus Engine turns exported chats into a governed local corpus: provider import, validation, source freshness, federation, policy replay, candidate diffs, and grounded retrieval.

3. The H1 commercial surface is now packaged for MCP hosts:

```bash
pipx install "conversation-corpus-engine[mcp]"
cce-mcp --project-root /path/to/project
```

4. The first tools are intentionally narrow: search a corpus with citations, list registered corpora, and export MCP-facing project context.

5. The first audience is people whose AI conversations contain actual decisions: builders, researchers, consultants, and small teams.

## Launch Checklist

- [ ] Confirm PyPI package ownership and upload credentials.
- [ ] Publish package.
- [ ] Replace install command if a shorter PyPI alias is created.
- [ ] Deploy `docs/commercial/h1/landing-page/`.
- [ ] Post Hacker News launch.
- [ ] Post social short and thread.
- [ ] Record inbound feedback in the future ORGAN-II surfaces repo.
