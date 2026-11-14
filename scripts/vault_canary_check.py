#!/usr/bin/env python3
"""Read-only planted-marker scan; install CCE or use PYTHONPATH=src from a checkout."""

from conversation_corpus_engine.archive_sealed_canary import main

if __name__ == "__main__":
    raise SystemExit(main())
