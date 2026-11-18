# KNOVV acceptance audit (sanitized)

This is a public, method-focused summary of an independent, read-only acceptance audit of a KNOVV status report against live state. It omits real test counts, commit hashes, receipt names, private paths, and other sensitive measurements. Detailed evidence was retained privately.

## Scope and method

- Verified merge/live-state claims against repository and deployment state available at audit time.
- Reproduced documented validation gates locally where possible at the relevant commits/environments.
- Distinguished **merged**, **installed**, and **run-on-real-data** as separate states.
- Recorded no secrets or private content.

## Findings (summary)

- **Merged state**: reviewed changes were merged in the relevant repositories as claimed at the time of audit.
- **Local gates**: most documented checks reproduced locally; some failures were environment- or test-hermeticity-related and were tracked for hardening.
- **Live vs merged**: installed/runtime state lagged merged source in places, and no real-source production run or private-corpus publication was established by the audit.
- **Report accuracy**: several claims or bounds in the status report were found to be overstated or insufficiently isolated by tests; follow-up hardening work was identified.
- **CI/CodeQL**: hosted check failures were attributable to external billing/runner conditions rather than code defects, based on observed job behavior.

## Hardening follow-up

Hardening work proceeded in separate, test-first branches with independent review before PRs, covering sealed publication preflight/fingerprinting, capture hardening, archive receipt behavior, history handling, and strict-profile correctness. End-state planning details that included sensitive aggregate information were kept in private records.
