# Contributing

Open an issue before a large behavior or workflow change. Keep pull requests focused,
describe the trigger and resulting behavior, and avoid committing generated media,
databases, logs, credentials, model weights, or machine-specific paths.

## Development environment

For full application development, use `python setup_project.py` and its `.venv`.
Ollama and ComfyUI are separately installed services; model downloads are optional
and are not needed for offline tests.

For a small offline-only environment, create `.venv` with `python -m venv .venv`,
then use its Python to run `-m pip install -r vps/requirements.txt`. This installs
the HTTP/image dependencies used by the verifier, without voice recognition models
or GPU dependencies. It is not a complete runtime installation for media generation.
Use Python 3.11 or newer. CI checks Python 3.11 and 3.12 on Linux; a configured
workflow is not evidence that those jobs or any GPU path have passed.

## Issues and pull requests

Use the bug-report or feature-request issue template. Include sanitized diagnostics
and OS/dependency versions for bugs, and the user problem for feature requests.
Report vulnerabilities through [the private security process](SECURITY.md).
Fork the project, make a focused branch, run the checks below, and open a pull
request against `main` using the PR template. State which checks passed, failed,
or were not tested; never attach private media, credentials, or your database.

## Verification

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
