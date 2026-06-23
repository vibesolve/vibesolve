# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in vibesolve, please report it
**privately** rather than opening a public issue.

Email **hello@vibesolve.ai** with:

- A description of the issue and its potential impact
- Steps to reproduce (a minimal example if possible)
- Any suggested remediation

We aim to acknowledge reports within a few business days and will keep you
informed as we work on a fix.

## API keys & secrets

vibesolve calls third-party LLM providers and requires API keys to run. To keep those keys safe:

- Keep keys in `.env.local` (gitignored) or in environment variables — **never** commit them, and never put them in `config.yaml`.
- If a key is ever exposed (committed, logged, or shared), **rotate it
  immediately** at the provider (OpenAI / Anthropic).
- Generated project artifacts under `results/` and logs under `logs/` may echo parts of your input — review before sharing them publicly.

## Supported versions

Security fixes are applied to the latest release.
