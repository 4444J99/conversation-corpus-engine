# Show HN: Search all your AI conversations from one place

_Ready-to-post copy for Hacker News and short-form social. External posting requires an authenticated account and is not performed by this repo._

**Suggested URL:** https://github.com/4444J99/conversation-corpus-engine
**Landing page:** https://4444j99.github.io/conversation-corpus--surfaces/
**Surfaces repo:** https://github.com/4444J99/conversation-corpus--surfaces

---

Hi HN,

I built Conversation Corpus Engine (CCE), a local-first Python CLI and MCP server that turns AI chat exports into one searchable, governed corpus.

The problem: after a few months, the useful conversations I had with ChatGPT, Claude, Gemini, and others are effectively gone. They live in separate provider accounts, in formats I don't control, with no way to search across them or tell which ones are still relevant. I wanted my past reasoning to be something I could query and cite, not something I have to scroll through.

What CCE does today:

- Imports exports from ChatGPT, Claude, Gemini, Perplexity, Copilot, and Grok into a provider-neutral corpus (more adapters are pluggable).
- Validates and fingerprints sources, tracks freshness, and records which corpus is authoritative per provider.
- Federates multiple corpora, replays promotion policies, and diffs candidate corpora before replacing a live one.
- Answers questions from the corpus with citations instead of loose keyword hits, via CLI or a read-only stdio MCP server (`cce_search`, `cce_list_corpora`, `cce_surface_context`).
- Emits versioned JSON schemas and surface bundles so other tools can consume the same artifacts.

Everything runs locally against your own exports. The repo contains code, contracts, tests, and redacted fixtures only — no conversation data, browser profiles, or credentials.

Install from the tagged release:

```bash
pipx install "conversation-corpus-engine[mcp] @ git+https://github.com/4444J99/conversation-corpus-engine.git@v0.3.1"
cce-mcp --project-root /path/to/project
```

Repo: https://github.com/4444J99/conversation-corpus-engine
Landing page: https://4444j99.github.io/conversation-corpus--surfaces/

This is early work, and I'd especially like feedback from people whose AI conversations contain actual decisions — researchers, engineers, consultants, and small teams trying to preserve decision history across tools. Questions about export formats, retrieval quality, or what "good governance" should mean for personal archives are all welcome.

---

## Short social version

I shipped a local-first Python CLI and stdio MCP server for searching AI conversation exports across providers, with citations, source freshness, validation, and corpus federation. Early feedback welcome: https://github.com/4444J99/conversation-corpus-engine

## Thread version

1/ AI conversations are becoming work product, but they're scattered across providers and hard to find after the fact.

2/ Conversation Corpus Engine imports chat exports into a local, provider-neutral corpus with validation, source freshness, federation, policy replay, and cited retrieval.

3/ The MCP server is read-only and exposes three tools today: `cce_search`, `cce_list_corpora`, `cce_surface_context`.

4/ Install from the tagged release: `pipx install "conversation-corpus-engine[mcp] @ git+https://github.com/4444J99/conversation-corpus-engine.git@v0.3.1"`

5/ Repo: https://github.com/4444J99/conversation-corpus-engine — looking for feedback from people who treat AI chats as durable work product.

## Posting checklist

- [ ] Confirm the landing page is deployed at the URL above (or update it here).
- [ ] Confirm the tagged `v0.3.1` install command still resolves.
- [ ] Post the HN text from a human-owned account; answer comments directly.
- [ ] Record the public post URL and notable feedback in issue #20.
