# Contributing to Sprint

Use Python 3.12+ and uv. Explain the problem, resulting behavior, and validation in each change. Keep the authoring API small and runtime responsibilities explicit.

Run focused unit tests and Ruff checks. Run provider-backed preflights when changing isolation, resource lifecycles, or artifact transfer. Keep credentials and run output out of Git.

Sprint retains Harbor's execution runtime under `src/harbor`. Preserve its license and attribution; upstream contribution guidance is retained in `reference/harbor/CONTRIBUTING.md`.
