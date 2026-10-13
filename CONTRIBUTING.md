# Contributing

Open an issue before a large behavior or workflow change. Keep pull requests focused,
describe the trigger and resulting behavior, and avoid committing generated media,
databases, logs, credentials, model weights, or machine-specific paths.

Run `python scripts/verify.py` and `git diff --check` before submitting. For a change to
Telegram delivery, Ollama behavior, or ComfyUI wiring, also report the live path tested,
the relevant HTTP/job result, and the produced artifact where appropriate. Mocked and
offline checks must be labeled as such.

Follow `AGENTS.md` for architecture invariants and portability rules. Workflow changes
must include the API-format JSON, its code registration, and corresponding contract
checks. Do not modify third-party installations or vendor code as part of a project fix.

For performance-planning changes, run the focused suite before the full verifier:

```text
python -m unittest discover -s tests -p "test_performance*.py"
python -m unittest discover -s tests -p "test_multichar*.py"
```

Use the Python interpreter from the environment created by setup: `.venv/Scripts/python.exe`
on Windows or `.venv/bin/python` on macOS/Linux. The canonical verifier uses temporary
databases and local fake engines; it does not generate media or contact live services.
Passing these tests establishes planning and prompt contracts, rather than rendered
speech or character-motion quality.

Release ZIPs are built with `python scripts/build_release.py --name CastWorkshop-v0.9.0-beta.1`.
The builder refuses to overwrite an existing candidate. Select a fresh output directory
when rebuilding the same version. See [the release checklist](docs/RELEASE_CHECKLIST.md).
