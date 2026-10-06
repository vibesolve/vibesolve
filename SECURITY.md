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

VibeSolve calls third-party LLM providers using API keys, provider credential
chains (for example AWS or Google Cloud) or Pi logins. To keep those credentials safe:

- Keep keys in `.env.local` (gitignored) or in environment variables — **never** commit them, and never put them in `config.yaml`.
- Keep Pi login credentials in its private store; never copy tokens into the repository or logs.
- If a key is ever exposed (committed, logged, or shared), **rotate it
  immediately** at the issuing provider; for a Pi login, log out and back in.
- Generated project artifacts under `results/` and logs under `logs/` may echo parts of your input — review before sharing them publicly.

## Supported versions

Security fixes are applied to the latest release.
