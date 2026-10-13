# CastWorkshop

**Build a persistent local cast of AI characters, then turn them into conversations,
voices, images, and multi-character video scenes.**

CastWorkshop is an open-source, local-first character and media environment. Create
characters, talk with them, give them voices, and place them in scenes. It brings
saved profiles, identity and voice references, conversation context, and media
creation into one workflow, so a new scene can start from the cast you already built.

[Requirements](#requirements) | [Install](#install) |
[Getting started](docs/GETTING_STARTED.md) | [Release status](#release-status) |
[Security](SECURITY.md)

## Why CastWorkshop?

- **Persistent characters:** keep profiles, identity references, voices, conversation
  history, and media references beyond a single prompt.
- **Chat-to-media continuity:** carry saved character references and conversation
  context into images and video rather than rebuilding each request in separate tools.
- **Multi-character scenes:** create two-character images and videos with named
  participants, identity and voice references, speaker handling, and authored dialogue.
- **Scene continuity:** approve a visual scene anchor for later media or continue a
  video from its ending frame.
- **Local-first orchestration:** coordinate locally run AI services while keeping
  application state and generated media on your machine.
- **Web + optional Telegram:** work through the browser or bot with the same saved cast.

## How it works

Ollama writes the conversation and plans media. ComfyUI runs the image workflows,
MOSS voice generation, and MiniMax H3 talking-video workflows. A local Flask and
SQLite service queues durable work so long GPU jobs do not block Telegram polling.

## Components

- `vps/app.py`: Telegram bot, settings, sessions, job queue, and durable delivery outbox
- `worker/worker.py`: Ollama and ComfyUI orchestration
- `scripts/supervisor.py`: process ownership, health state, and bounded child recovery
- `worker/workflows/`: the supported ComfyUI API workflows

CastWorkshop defaults to this computer only (`127.0.0.1`). Setup offers an explicit
trusted-network option (`0.0.0.0`); `start.py` prints network URLs only when enabled.
The interface has no built-in login or HTTPS;
see [SECURITY.md](SECURITY.md) before exposing it beyond your own devices. Ollama and
ComfyUI can remain on localhost; generated media and the database stay on this computer.

## Requirements

- Python 3.11 or newer
- Ollama with a suitable chat model
- ComfyUI with the nodes and model files listed in `docs/MODELS.md`
- FFmpeg for continuing videos from their ending frame (video setup can install a bundled binary and configure `FFMPEG_PATH`)
- A Telegram bot token from BotFather (optional, for Telegram transport)
- A GPU supported by the selected ComfyUI workflows with sufficient memory

Windows, macOS, and Linux use the same Python entry points. GPU workflow support still
depends on what ComfyUI and the selected models support on the host.

### Hardware and storage

- **Model downloads:** approximately 42.5 GB for images and 45.4 GB for video/MOSS
  voice, or 87.9 GB combined before reuse. Setup searches ComfyUI's active model folders and skips exact filenames with matching
  sizes (0.1% tolerance, capped at 1 MiB), without hashing models. Allow additional space for ComfyUI, Python environments, temporary
  files, and generated media. See [the full inventory](docs/MODELS.md).
- **Validation:** This beta's offline suite has been exercised on Windows. A completed beta
  GPU acceptance run and minimum VRAM requirement have not been verified; Python
  tests do not establish GPU compatibility.
- **Compatibility:** the Python entry points are intended for Windows, macOS and
  Linux. GPU support depends on ComfyUI, the models and their backend. macOS/Linux
  GPU paths remain untested in this release evidence.

## Install

Clone or download the repository into a directory named `CastWorkshop`, then run:

```text
cd CastWorkshop
python setup_project.py
```

The setup script creates `.venv`, installs both requirements files, initializes the
SQLite database, and offers an optional Telegram token prompt when `.env` does not exist.
Leave the token blank for web-only use. Setup also offers a network-access choice,
defaulting to this computer only. Headless setup does not prompt and honors an explicit
`API_HOST` configuration, otherwise choosing `127.0.0.1`. You can
instead copy `.env.example` to `.env` and edit it yourself.

Choose setup's optional dependency downloads or install them using `docs/MODELS.md`, start Ollama and ComfyUI,
then launch the application:

```text
python start.py
python scripts/inspect_env.py
```

Setup can optionally install the configured Ollama model, image models/nodes, and video plus MOSS voice
models/nodes in separately selected groups. SNOFS is included in the image group
and enabled only in adult mode. Model downloads are checked by size
and resumable; restart ComfyUI after installing nodes. See `docs/MODELS.md` for
publisher terms and any required download tokens. The web Settings panel can
check the selected Ollama model and the workflow files visible to ComfyUI. Look
candidates use `krea2_turbo_int8_convrot.safetensors`. Native identity images and
scene insertion use `krea2_turbo_fp8_scaled.safetensors`; both are installed by
the image dependency group.

For media that should remain in one setting, select a character and use **Scene** in
the conversation header. Scene continuity is opt-in and character-specific; the latest
image or video request takes precedence when it changes an established detail.
After saving a scene, **Generate candidate** creates a possible visual anchor from the
character's original reference. Open the completed image and choose **Use as scene
anchor** to approve it. Later single-character media branches from that approved image
rather than from previous results, which limits cumulative visual drift. Changing the
scene description clears an incompatible anchor; removing an anchor returns to
text-only continuity.

Completed videos in the web conversation include **Continue video**. CastWorkshop
extracts a frame just before the source clip ends and uses it as the opening visual
anchor for the next clip. Single-character continuations retain the saved voice;
two-character continuations also retain both original identity and voice references.
Two-character continuations currently support motion-only requests.

Stop or restart it with `python stop.py` or `python restart.py`. Logs are written to
`logs/`; application state is stored in `vps/jobs.db`. Both are ignored by Git.

## Release status

**CastWorkshop 0.9 Public Beta** (`0.9.0-beta.1`) is the initial source candidate,
prepared for private acceptance before public release. The eventual tag is
`v0.9.0-beta.1`; the source archive is `CastWorkshop-v0.9.0-beta.1.zip`.
See [CHANGELOG.md](CHANGELOG.md) for changes
and known limitations. Offline tests do not establish rendered speech or motion quality;
GPU generation and fresh-install checks remain part of release QA.

See [Performance planning](docs/PERFORMANCE_PLANNER.md) for timeline, fallback and
audio-boundary behavior in two-character videos.

## Verification

`python scripts/verify.py` runs the portable offline gate: Python compilation, workflow
JSON validation, smoke checks, and unit tests. It does not contact Telegram, Ollama,
ComfyUI, or a GPU. See `docs/RELEASE_CHECKLIST.md` for live release validation.

## Content controls

The `/mode` command selects SFW or consenting-adult NSFW behavior. The application must
reject sexual content involving minors and consent violations. Operators are responsible
for following the laws and platform rules that apply to their use.

See [Getting started](docs/GETTING_STARTED.md) for configuration and troubleshooting,
and [Contributing](CONTRIBUTING.md) before submitting changes.

## License

CastWorkshop is licensed under the Apache License, Version 2.0. See `LICENSE`.
