# CastWorkshop contributor instructions

Read this file before changing the project. Keep changes portable across Windows,
macOS, and Linux, and never claim a fix without observed verification output.

## Architecture

- `vps/app.py` owns Telegram polling, the HTTP job queue, SQLite state, and delivery.
- `worker/worker.py` polls the VPS and drives Ollama and ComfyUI.
- `scripts/supervisor.py` owns both processes and restarts observed child exits.
- Workflows used by the worker live under `worker/workflows/`.

Run the stack from the repository root with `python start.py`, `python stop.py`,
or `python restart.py`. Runtime state belongs in ignored `logs/` and `vps/jobs.db`.

## Engineering contract

Before committing an application change, run:

```text
python scripts/verify.py
git diff --check
git diff
```

The verifier must remain offline and must not require Telegram, Ollama, ComfyUI,
a GPU, or one particular operating system. For media or integration changes, also
exercise the real affected path when those local services are available. Clearly
distinguish offline checks from live integration results.

Keep changes focused. Do not silently change workflow model or sampler settings,
discard user work, weaken a failing test, or edit external ComfyUI installations.

## Invariants

- The VPS is the sole SQLite writer. Do not hold `db_lock` during network or model calls.
- Completion and delivery are idempotent. Preserve generated media until delivery is durable.
- Do not blindly resubmit an ambiguous ComfyUI request. Renew leases for long jobs.
- Only one process may poll a Telegram token. Never print tokens or secrets.
- Conversation and media planning use separate calls to the same resident LLM. Evict it
  before GPU media generation.
- Media must match the latest reply and request: action, clothing state, participants,
  setting, and identity references must survive planning and validation.
- Use single-character workflows for one visible person and multi-character workflows
  for multiple visible people. Validate workflow node IDs before changing their wiring.
- MOSS is the active voice engine and must unload its models after generation.
- Use `pathlib`, `sys.executable`, and subprocess argument lists. Restrict platform
  branches to process boundaries and always provide Windows, macOS, and Linux behavior.
- Sexual content involving minors is prohibited. Consenting-adult content is supported.

## Common change points

- Register workflows in `vps/app.py` and update the workflow contract checks.
- Keep the database setting and code default aligned when changing an active engine.
- Register Telegram commands in `setup_telegram_commands()` in `vps/app.py`.
- Mirror the real session state and commit enqueued jobs in integration fixtures.
- Ollama requests must disable thinking and strip accidental thinking blocks.
