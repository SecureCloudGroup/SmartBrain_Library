# Contributing to the SmartBrain Library

## Most people: use the app
Inside SmartBrain, open **Neural Interface → Library**. From there you can:
- **Tap Yes** when SmartBrain offers you a source. Each Yes counts as a vote that the source is good.
- **Add a source** with the form. It works for you straight away, as a local source. You can also send it to the Library as a suggestion.

You don't need Git or a GitHub account. Suggestions arrive through the Library API. Each one is validated, then reviewed by the maintainer.

## What the Library accepts
- Data that many people would want on a live dashboard (see `taxonomy/taxonomy.json`).
- Sources whose terms allow automated personal use. Nothing that requires a paid plan, for now.
- https endpoints on public hosts. A free key is fine; the user enters their own key in the app.
- No keys, tokens or personal data in the record. The schema check rejects them.

## Maintainer workflow
1. Changes arrive through `sourcetool ingest`, `sourcetool harvest` or hand edits to `sources/curated/*.jsonl`.
2. Run `python -m sourcetool check` and `python -m sourcetool validate --id <id>`.
3. Open a PR. CI schema-checks every record and probes the changed ones.
4. Only the maintainer merges into `main`. A tag publishes a new pack; each app release pins that pack's sha256 (Ed25519 signing is planned, see `docs/PLAN.md` R12).

Please report security issues privately (see [SECURITY.md](SECURITY.md)).
