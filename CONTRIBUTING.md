# Contributing to VibeSolve

Thank you for your interest in contributing! This document covers how to set up a development environment, the workflow for submitting changes, and the project's conventions.

## Prerequisites

- Python 3.11+
- [Conda](https://docs.conda.io/en/latest/) (recommended) or a virtualenv
- Docker (required for validation; skip with `--no-validation-loop` during development)
- An OpenAI API key (or an Anthropic key, for `--provider claude`)

## Development Setup

```bash
# 1. Clone the repository
git clone http://github.com/vibesolve/vibesolve.git
cd vibesolve

# 2. Create and activate the environment
conda create -n vibesolve python=3.11 -y
conda activate vibesolve   # or: python -m venv .venv && source .venv/bin/activate

# 3. Install in editable mode, with dev/test dependencies
pip install -e ".[dev]"

# 4. API key — gitignored, never committed
cp .env.example .env.local
# Now open .env.local and set OPENAI_API_KEY=sk-... (add ANTHROPIC_API_KEY for --provider claude)

```

> **Docker validator image:** The `timefold-validator` image is built automatically on first use via `build_image_if_needed()` in `docker_validator.py`. You can also build it manually upfront to avoid the wait on first run:
> ```bash
> docker build -t timefold-validator docker/
> ```
> Rebuild it whenever you change `docker/Dockerfile` or `docker/pom-warmup.xml`.

## Running the Pipeline

```bash
conda activate vibesolve   # once per shell
# start Docker — Linux: sudo systemctl start docker  |  macOS/Windows: launch Docker Desktop

vibesolve run            # the bundled example
vibesolve run --serve    # also emit a Dockerfile + docker-run.sh
vibesolve batch          # every *.txt in user_input/, in parallel
```

For shell tab-completion, run `vibesolve --install-completion` once (edits your personal shell config).

## CLI reference

CLI flags override `config.yaml` and environment variables. Run `vibesolve run --help` / `vibesolve batch --help` to see this same list.

### `vibesolve run [FILE]`

`FILE` — problem description text file (default: `user_input/timetable.txt`).

| Flag | Default | Description |
|---|---|---|
| `--serve` | off | On success, emit `Dockerfile` + `docker-run.sh` into the generated project. |
| `--user-validate` | off | Pause after parsing to review and correct the spec before code generation. |
| `--config PATH` | `config.yaml` if present | YAML config file. |
| `--provider openai\|claude` | `openai` | LLM provider. |
| `--reasoning-effort low\|medium\|high` | per-agent config | Override reasoning effort for all agents at once. |
| `--max-iterations N` | `max_fix_iterations` (10) | Max fixer agent iterations. |
| `--no-validation-loop` | off | Skip the Docker validation/fixer loop. |

### `vibesolve batch [FILES...]`

`FILES` — input problem files (default: all `*.txt` in `--input-dir`).

| Flag | Default | Description |
|---|---|---|
| `--input-dir PATH` | `user_input` | Directory to scan for `*.txt` files. |
| `--serve` | off | Emit `Dockerfile` + `docker-run.sh` into each successfully-generated project. |
| `--config PATH` | `config.yaml` if present | YAML config file. |
| `--provider openai\|claude` | `openai` | LLM provider. |
| `--workers N` | `default_workers` (3) | Number of parallel workers. |
| `--max-iterations N` | `max_fix_iterations` (10) | Max fixer iterations per problem. |
| `--no-validation-loop` | off | Skip the Docker validation/fixer loop. |

`batch` has no `--reasoning-effort` or `--user-validate`; `run` has no `--workers` or `--input-dir`.

## Tests

Run the test suite with:

```bash
pytest
```

The tests are offline — no API key or Docker required — and cover the CLI surface, settings resolution, and the core model/merge logic. Please add or update tests when changing that behavior.

## Code Style

- **Type annotations:** required on all public functions and methods
- **Comments:** only when the *why* is non-obvious; avoid restating what the code does


## Submitting Changes

1. Create a branch from `master` (fork first if you don't have push access):
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Keep commits focused — one logical change per commit.

3. **Open a pull request** against `master`. Include in the PR description:
   - What the change does and why
   - How you tested it (e.g., which problem input, what output you observed)

## Adding or Modifying Agent Prompts

Agent prompts live in `src/vibesolve/prompts/*.txt`. When modifying a prompt:

- Document your reasoning in the PR description (prompts are hard to review without context)
- Run the pipeline end-to-end on at least one problem to verify the change doesn't regress output quality
- Note which agent(s) the prompt serves (see the agent table in [ARCHITECTURE.md](ARCHITECTURE.md))

## Reporting Issues

Please use [GitHub Issues](http://github.com/vibesolve/vibesolve/issues) to report bugs or request features. Include:

- The problem input file (or a minimal reproduction)
- The full error output from the pipeline log (`logs/run_<timestamp>/pipeline.log`)
- Your Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the same [MIT](LICENSE) license that covers this project.
