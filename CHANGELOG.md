# Changelog

## RC7 - model consolidation and selected voice preservation

- Voice selection trims the introduction from the selected candidate recording
  instead of synthesizing a second voice reference. CPU word timestamps locate
  the neutral passage; FFmpeg retains the remaining original performance.
- Setup installs the voice-trimming dependencies, verifies FFmpeg, and caches the
  recognition model in the application's environment.
- Appearance candidates and image workflows share Krea2 FP8, the Qwen3-VL 4B FP8
  encoder, and the Qwen image VAE. Workflow samplers and other settings are preserved.
- Removed the redundant Krea2 INT8 checkpoint and Huihui image encoder, plus the
  retired realism and filter-bypass adapters, from setup and readiness checks.
- Image downloads are approximately 22.2 GB; image plus video/voice downloads are
  approximately 67.6 GB, excluding the Ollama chat and Whisper recognition models.

Live voice-boundary accuracy and rendered appearance/quality still need validation
with ComfyUI. Existing downloaded models and saved character references are retained.

## 0.9.0-beta.1 - CastWorkshop 0.9 Public Beta

Initial source candidate for private acceptance. Tag `v0.9.0-beta.1` and public
release publication are deferred until the applicable acceptance gates pass.

- Web and optional Telegram character chat, scene continuity, images, voice samples,
  and single- or two-character videos.
- Root `setup_project.py` entry point with optional dependency groups, headless
  configuration, setup logs, and an optional interactive launch.
- Local-only networking (`127.0.0.1`) is the default. Interactive setup offers LAN
  access (`0.0.0.0`) with a warning; headless setup honors explicit `API_HOST` values
  without prompting. Runtime honors that configuration and keeps local worker traffic
  on loopback. Ollama and ComfyUI may remain local.
- Validated performance plans with per-character behavior, dialogue/timing checks,
  one repair attempt, and deterministic fallback. Exact authored dialogue is retained.
- Scene-attached two-character video openings use the same two-pass insertion path
  as two-character images. Opening-frame wardrobe is established before the video.
- Heartbeat-confirmed child process ownership and recovery safeguards for ambiguous
  ComfyUI submissions. No automatic video retries were added.
- Source-only release packaging with per-file hashes and archive checksums.

Known limitations: the HTTP interface has no login or TLS; use trusted networks.
Generated speech, listener motion and identity consistency still need live QA.
Dependency versions are not fully locked, and cross-platform GPU/fresh-install
coverage is incomplete. Model and sampler settings remain unchanged.
