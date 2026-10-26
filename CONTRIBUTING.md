# Contributing

Changes should make the skill easier to apply and its examples safer to run. Russian and English issues are welcome.

## Before changing behavior

1. Read `SKILL.md` and the reference for the affected area. Preserve progressive disclosure: API details, tooling and complete examples do not all belong in the core prompt.
2. Check the official Telegram contract and the SDK version. Date version-dependent claims; do not present an API snapshot as permanently current.
3. Reproduce the consumer-visible problem with a small payload or local scenario. Add a deterministic behavior test for a plausible regression, not a source-text or exact-copy assertion.
4. Keep the example's scope explicit: one polling process, SQLite, no live credentials in tests. Do not introduce Redis, a web dashboard or paid services just to demonstrate a simple workflow.

## Verify a change

From the repository root:

```bash
python -m pip install -r requirements-dev.txt -r examples/support-bot/requirements.txt
python scripts/check_skill.py --json
python -m unittest discover -s tests -v
```

From `examples/support-bot`:

```bash
python -m unittest discover -s tests -v
python -m support_bot --offline-smoke
```

For distribution changes, build both archives and run `python scripts/verify_archives.py dist` as described in the README. Review the selected files before sharing them.

## Pull requests

Explain the problem, changed behavior, exercised checks and limitations. Include a minimal example for formatting/validator changes. Update both READMEs when public usage changes. A screenshot of the gallery is evidence of the gallery only, not Telegram rendering.

Never submit tokens, `.env`, working databases, session strings, logs with user messages or machine-specific project state. See [SECURITY.md](SECURITY.md). Licensing has not been selected; no license grant should be assumed or added without the owner's decision.
