# Sprint development

Sprint is the root framework. Use `uv` and Python 3.12+.

- `src/sprint/`: authoring API, runtime, resource broker, validation, review records and provider/agent adapters.
- `examples/sprint/`: runnable environments and preflights.
- `tests/sprint/`: Sprint unit tests.
- `sprint-docs/`: current documentation.
- `reference/harbor/`: upstream documentation and attribution.

Keep environments, tasks, validation, agents and attached resources separate. Provider credentials and protected validation data must stay outside learner images. Never commit runtime records or credentials.

Use explicit runtime guards rather than asserts; prefer asyncio.TaskGroup for concurrency and Path methods for file I/O. Follow CONTRIBUTING.md.

Keep tests compact: cover core behavior, trust boundaries and consequential regressions. Reuse existing coverage; avoid exhaustive permutations, implementation-mirroring tests and tests for cosmetic changes. Run relevant unit tests for changes. Integration preflights are necessary when runtime boundaries change. Do not test CLI help formatting. Run `uv run --no-default-groups --with ruff ruff check --fix .`, `uv run --no-default-groups --with ruff ruff format .`, and `uv run --no-default-groups --with ty ty check` after code changes. Record existing upstream type failures honestly.

Focused tests:

```sh
uv run --no-default-groups --with pytest --with pytest-asyncio pytest tests/sprint tests/unit/test_trial_verifier_separate.py -q
```

Add a concise CHANGELOG.md Unreleased entry for major features or breaking changes only. Preserve Apache-2.0 attribution when changing retained upstream code.
