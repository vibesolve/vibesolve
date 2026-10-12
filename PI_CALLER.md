# Pi completion caller

VibeSolve uses Pi 0.84.2 for model calls. Python runs the pipeline, builds prompts,
selects models, validates responses and applies file changes.

## Contract and limits

`PiAgentCaller.call_typed()` sends system/user text, output schema, provider,
model/effort, timeout and request ID to a private Node worker. Each attempt calls
`ModelRuntime.completeSimple` once. Calls have no tools or conversation history;
each receives only the context Python supplies.

OpenAI Responses/Codex use native strict JSON schema. Fields with defaults are
required in the response. Other providers receive schema instructions in the
system prompt. Python repairs JSON locally and validates it with Pydantic;
the caller never merges files.

Empty, malformed, schema-invalid or truncated responses are retried, up to three
attempts total, with the same input, model and effort. The SDK retries provider
errors at most twice per attempt.

| Limit | Tokens in / out | Time |
|---|---|---|
| Per typed call | 400k / 40k | 600 s |
| Per problem (worker) | 1.5M / 100k, 70 completions | 1800 s spent in completions |

Limits are checked before each request, so one response can cross a ceiling.
Usage is recorded for failed calls too, and missing cost data stays unknown.
Costs are Pi's API price estimates, not subscription charges or invoices.

## Transport sessions and caching

Each problem uses one private `sessionId` across roles, models and retries.
The worker requests WebSockets; Pi handles transport selection, reconnects and
SSE fallback. VibeSolve sets no provider-specific cache options. Reusing a
session does not guarantee cache hits.

Python closes each worker; EOF, protocol errors and SIGINT/SIGTERM abort calls
and exit the process, which closes its connections. Workers that fail to exit
are killed after a timeout. A worker that fails at the transport level is
replaced for the next stage.

## Installation and credentials

Node >=22.19 and npm are required. First use installs the lockfile-pinned worker
into a local cache keyed by the worker files' hash.

Pi reads provider environment variables, credential chains and its login store.
Provider credentials can be loaded from `.env.local`; existing environment
variables take precedence. `VIBESOLVE_API_KEY` overrides the key for providers that accept
one; it cannot replace a Pi login. Never commit credentials.

`openai` remains the default provider. To use an existing Pi Codex login, select
`--provider openai-codex` or set `provider: openai-codex` in YAML and leave
`VIBESOLVE_API_KEY` empty.

## Configuration

Runtime settings resolve as CLI > environment > YAML > `.env.local` > bundled
defaults. The root `config.yaml` auto-loads; `--config` chooses another file.
Bundled role/model/effort defaults live in
[`src/vibesolve/config/provider_models.json`](src/vibesolve/config/provider_models.json).

Use Pi provider/model IDs. Aliases are `claude → anthropic`, `gemini → google`
and `bedrock → amazon-bedrock`; use the same name across configuration files and
environment overrides. Unsupported model/effort combinations raise an error.

Set `provider_models.<provider>.<agent>.model` and `.effort` in YAML. A provider's
optional `_default` supplies model and/or effort to all roles; precedence is
per-agent value > `_default` > bundled default, field by field. Providers without
a bundled profile need `_default.model` or an explicit model for every role.
`fixer_cheap` uses the IO route and has no separate setting.

Nested environment overrides use double underscores, for example
`PROVIDER_MODELS__GOOGLE__REVIEWER__EFFORT=medium`. A provider-wide default needs
three before `DEFAULT`: `PROVIDER_MODELS__DEEPSEEK___DEFAULT__MODEL=deepseek-chat`.
Supported effort values are `auto`, `none`, `low`, `medium`, `high`; `auto` omits
the reasoning parameter. `run --reasoning-effort` overrides all roles at once;
otherwise configured efforts are preserved across retries.

## Tests

The offline tests in [CONTRIBUTING.md](CONTRIBUTING.md#checks) check response
validation, SDK payloads, usage, caching, connection reuse and worker cleanup.
Bundled profiles are checked against Pi's model catalog. These tests make no
model calls; they do not check account access or generated projects.
