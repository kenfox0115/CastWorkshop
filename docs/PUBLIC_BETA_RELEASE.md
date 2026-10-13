# CastWorkshop 0.9 Public Beta — source-beta release notes

Source version: `0.9.0-beta.1` · Status: **Public source beta**

The repository is public. Clone it or use **Code → Download ZIP** to install from source.
A packaged GitHub Release and version tag have not yet been published.

## Create characters that live beyond a single prompt

CastWorkshop brings persistent character profiles, appearances, saved voices,
conversation history, and media generation into a local browser interface with
optional Telegram access. Ollama plans conversations and media; ComfyUI runs the
image, MOSS voice, and MiniMax H3 video workflows.

## Included features

- Character creation, look candidates, voice audition/selection, and saved personalities.
- Single-character chat, images, and videos; two-character images and videos with
  named participants and reviewed speaker assignments.
- Scene libraries and approved visual anchors; continuation from a previous video's
  ending frame. Two-character continuation currently supports motion-only requests.
- Durable queues, delivery recovery, diagnostics, and process supervision.
- Light-themed UI and updated browser icon. Source release packaging includes the
  branding stylesheet and generates a file manifest and ZIP checksum.

## Install and dependencies

This is a source-based beta, not a standalone installer. See [Getting started](GETTING_STARTED.md)
and [Models](MODELS.md). Install Python 3.11 or newer, Ollama, and ComfyUI separately.
Run `python setup_project.py`, restart ComfyUI after installing nodes, start both
AI services, then run `python start.py`. Leave Telegram blank for browser-only use.

Image models need approximately 22.2 GB and video/MOSS voice models 45.4 GB:
67.6 GB combined before reuse, excluding Ollama and Whisper models, environments,
ComfyUI, caches, and generated media. A compatible GPU is required for the selected
workflows. A suggested starting configuration is 16 GB GPU memory (or equivalent) and 64 GB
system RAM. These are planning targets; no validated minimum VRAM or hardware matrix
is published.

## Validation and limitations

Local preparation on Windows with Python 3.13 passed 82 source compilation checks,
23 workflow JSON checks, 61 smoke checks, and 436 unit/integration tests (one symlink
test skipped). Isolated real start/restart/stop also passed with worker and Telegram
disabled. These are offline/synthetic results, not GPU acceptance evidence.
Python 3.11/3.12 Ubuntu CI runs on main pushes and pull requests; consult
[Actions](https://github.com/kenfox0115/CastWorkshop/actions) for the exact revision.
Both jobs passed for the initial public source revision `5bb0446`. Linux/macOS GPU paths, complete fresh setup, and
recent stop changes across operating systems still need owner-managed acceptance evidence.

Image identity consistency, speech accuracy, motion, and voice-trimming boundaries
require live review. Two-character performance plans span 5–10 seconds. Python
dependencies are not fully locked. Confirm the exact tested revision and hardware
in the final release description before publishing.

## Security and third-party terms

The HTTP interface has no login or HTTPS. Keep the default loopback binding; LAN
mode is for trusted networks. Do not expose port 8787 to the public internet without
external authentication and transport security. See [Security](../SECURITY.md).
Setup downloads dependencies/models; optional Telegram transport uses Telegram.

Source code is Apache-2.0. Models and nodes have separate publisher terms; weights
are not included. Operators must review those terms and applicable content rules.

## Feedback and assets

Use the repository's bug/feature issue templates with sanitized diagnostics.
Send vulnerabilities through the private reporting process in SECURITY.md.

Future packaged release assets: `CastWorkshop-v0.9.0-beta.1.zip` and its
`.zip.sha256` checksum. The ZIP includes `release_manifest.json` with per-file hashes.
Attach only the archive generated from the revision that passes acceptance checks.
