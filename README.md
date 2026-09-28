<p align="center">
  <img src="vs-icon.svg" alt="VibeSolve" width="120" />
</p>

# VibeSolve

Describe an optimization problem in plain English. Generate a
[Timefold Solver](https://timefold.ai/) Quarkus project with Java domain classes,
constraints, REST endpoints, tests and solver configuration, validated in Docker.

> Schedule lessons across rooms and timeslots so teachers, students and rooms
> never have overlapping lessons, and each class's lessons are spread across
> the week.

## Setup

Install [uv](https://docs.astral.sh/uv/), Node >=22.19 with npm, and Docker.
Python 3.11+ is required; uv can install a suitable interpreter.

```bash
git clone https://github.com/vibesolve/vibesolve.git
cd vibesolve
uv sync
cp .env.example .env.local
# Open .env.local and set provider credentials, e.g. OPENAI_API_KEY.
```

Keep credentials out of Git and `config.yaml`. The default provider is `openai`.
VibeSolve installs its Pi worker on first use. To use an existing Pi Codex login,
pass `--provider openai-codex` and leave `VIBESOLVE_API_KEY` empty.
See [PI_CALLER.md](PI_CALLER.md) for provider setup.

Start Docker before running with validation. On Linux, use
`sudo systemctl start docker`; on macOS/Windows, launch Docker Desktop. The
validator image builds automatically on first use.

## Usage

```bash
uv run vibesolve run                              # bundled timetable example
uv run vibesolve run user_input/my-problem.txt     # your own problem
uv run vibesolve run --user-validate               # review/correct the parsed spec
uv run vibesolve run --serve                       # also emit portable Docker artifacts
uv run vibesolve batch --workers 3                 # all user_input/*.txt
```

For Google, set `GEMINI_API_KEY` in `.env.local`, then run
`uv run vibesolve run --provider google`. Other provider configuration and model
selection are covered in [PI_CALLER.md](PI_CALLER.md#configuration).

Run `uv run vibesolve run --help` or `uv run vibesolve batch --help` for all
options. If you prefer plain `vibesolve` commands, activate the environment with
`source .venv/bin/activate` (Windows: `.venv\Scripts\activate`).

Single-run artifacts land in `results/run_<timestamp>/`, including the project
directory and zip; logs go in `logs/run_<timestamp>/`. Batch runs use
`batch_<timestamp>/<problem>/` and include aggregate summaries.
With `--serve`, run `./docker-run.sh` inside the generated project to serve it.

## Configuration

The root `config.yaml` loads automatically; `--config other.yaml` selects another
file. Precedence is CLI > environment > YAML > `.env.local` > bundled defaults.
Set role-specific models and effort under `provider_models`; use `_default` for
a provider-wide override. See the [default models](src/vibesolve/config/provider_models.json)
and [configuration examples](PI_CALLER.md#configuration).

Repair uses the IO model first, then the fixer, with two total attempts by
default (`--max-iterations` overrides this). Reported costs are Pi catalog
estimates, not invoices or subscription charges.

## How it works

Parser → optional user review → Model Builder → Constraint Builder → IO →
Integrator → Reviewer → Docker validation → repairs if needed.
Python carries the original request and exact corrections through every later
role. Pi makes the model calls.

Validation checks compilation, standalone solver execution and tests.
See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline and batch benchmarks.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development checks and review guidance.
For security reports and credential handling, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
