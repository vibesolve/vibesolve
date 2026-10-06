# Contributing

Follow [README.md](README.md) for setup and credentials. For development, install
the test dependencies with `uv sync --extra dev`. Commands below use `uv run`,
so activating the virtual environment is optional.

## Checks

```bash
uv run pytest
npm ci --prefix src/vibesolve/pi_worker --ignore-scripts
npm test --prefix src/vibesolve/pi_worker
```

These offline checks need neither API keys nor Docker. Python tests cover
settings, CLI behavior, context, typed calls, merges and repair policy. Node
tests check SDK payloads, usage, connections, caching and bundled model profiles.
They use mocked responses, not live model calls or generated projects.

Pi upgrades must keep the worker manifest/lockfile and Python's
`pi_install._PI_VERSION` synchronized; tests enforce this. See
[PI_CALLER.md](PI_CALLER.md) for Node requirements and caller details.

For an end-to-end check, start Docker and run:

```bash
uv run vibesolve run user_input/timetable.txt
```

The validator image builds automatically on first use. Rebuild it after changing
`docker/Dockerfile` or `docker/pom-warmup.xml`:

```bash
docker build -t timefold-validator docker/
```

Use `--no-validation-loop` only to isolate prompt/caller behavior. It does not
verify compilation, solver execution or tests. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the pipeline and the separate batch benchmark pass.

## Changes and review

- Use type annotations on public functions and methods. Explain non-obvious
  reasons in comments; do not narrate the code.
- Follow [AGENTS.md](AGENTS.md) for generated-project invariants and source rules.
  Add/update tests when changing CLI, settings, caller or model/merge behavior.
- Keep commits focused on one reviewable change, with its tests. Put rationale,
  verification and known limitations in commit messages and the PR description.
- For prompt changes, identify affected roles and run an end-to-end problem.
  For comparisons, keep inputs, model routes, effort and budgets matched;
  report pass/fail outcomes as well as tokens and estimated cost. Say whether
  you ran the whole pipeline or resumed from saved outputs. Label estimated
  costs as estimates. Prompt text assertions do not check generated code.
- Open a pull request against `master`, describing what changed, why, how it
  was tested and any unresolved failures. Do not commit experiment outputs or
  credentials; review logs for sensitive input before sharing evidence.

Report bugs through [GitHub Issues](https://github.com/vibesolve/vibesolve/issues)
with a minimal input, relevant redacted errors, Python version and OS. Report
vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

Contributions use the project's [MIT license](LICENSE).
