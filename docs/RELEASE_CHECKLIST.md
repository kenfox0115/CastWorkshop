# Release checklist

The release owner performs these checks on a fresh clone or clean machine.
The current candidate is **CastWorkshop 0.9 Public Beta** (`0.9.0-beta.1`),
with source archive `CastWorkshop-v0.9.0-beta.1.zip`. Its eventual Git tag is
`v0.9.0-beta.1`; private upload does not create a tag or public release.

- Confirm the release includes the Apache License 2.0 `LICENSE` file and the README
  identifies that license.
- Inspect `git diff --cached --name-only` and `git ls-files`: credentials, databases,
  logs, generated media, environments and weights must not be tracked. `git status
  --ignored` may show these local files only as ignored.
- Run `python scripts/verify.py` and `git diff --check`.
- Run `python setup_project.py` in a fresh clone.
- Start Ollama and ComfyUI, then confirm `python scripts/inspect_env.py` reports HTTP 200
  for all three services and fresh heartbeats.
- Confirm a fresh database starts in SFW mode and the everyday command menu is ordered.
- Toggle `/debug` twice and confirm advanced commands appear and disappear for that chat.
- Complete one text-only conversation and confirm replies remain varied and coherent.
- Generate and receive one SFW image and one permitted NSFW image.
- Generate, promote, and use a voice sample.
- Generate and receive one single-character video.
- Generate and receive one two-character image in the active mode and aspect; check
  both identities, composition, and delivery.
- Generate and receive one two-character video; check speaker order, dialogue text,
  identity binding, and the absence of prompt fragments in speech.
- Stop and restart during a long job, then confirm recovery does not duplicate rendering
  or lose a completed delivery.
- Verify local documentation links and example commands. Review tracked source for
  credentials, machine-specific paths and private development notes.
- Check the source ZIP checksum, file manifest, and exclusions. Publish the ZIP and
  checksum as release assets, rather than committing archives or generated manifests.
- Tag the exact revision that passed live validation.
