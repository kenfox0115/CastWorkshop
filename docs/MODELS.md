# ComfyUI workflow dependencies

The repository does not contain model weights. These are the exact filenames referenced
by the supported workflow JSON at this revision. Place them in the corresponding ComfyUI
model directories, or edit every affected workflow to use an equivalent local filename.

## Diffusion models and checkpoints

- `krea2_turbo_fp8_scaled.safetensors`
- `minimax_h3_hybrid_fl2va_ref2va_b25-49_nvfp4.safetensors`

Look candidates, identity images, scene insertion and legacy image presets share
Krea2 FP8 and the Qwen3-VL 4B FP8 encoder in both content modes. Krea2
and MiniMax are selected through UNET loader nodes.

## Text encoders

- `qwen3vl_4b_fp8_scaled.safetensors`
- `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`

## VAEs

- `qwen_image_vae.safetensors`
- `minimax_h3_video_vae_fp16.safetensors`
- `minimax_h3_audio_vae_fp32.safetensors`

## LoRAs

- `krea2_identity_edit_v1_2.safetensors`
- `snofs_krea_v1_4.safetensors`

## Custom nodes

Most workflow node types are built into recent ComfyUI versions. The custom packages
needed for MOSS TTS, model unloading and the Clownshar sampler are listed below and
can be installed by setup. ComfyUI's missing-node report or Manager can help diagnose
packages that fail to register.

MOSS uses the `Voice Design 1.7B` variant (`OpenMOSS-Team/MOSS-VoiceGenerator`) and
`OpenMOSS-Team/MOSS-Audio-Tokenizer`. Setup installs both in
`models/TTS/moss_tts/<repository-name>/`, matching TTS Audio Suite's downloader.

The application defaults to the local Ollama alias `qwen3-14b-64k`. On a clean machine,
setup downloads the public source `huihui_ai/qwen3-abliterated:14b-v2-q8_0` and creates
that alias from `config/ollama/qwen3-14b-64k.Modelfile`. The alias is deliberately local;
`ollama pull qwen3-14b-64k` will fail because it is not an Ollama registry tag. `/model`
can select another installed model. A suitable replacement must follow structured JSON
instructions, support the desired consenting-adult content policy, and fit the machine's
latency and memory budget.

## Optional automatic installation

Run `python setup_project.py`. It offers the configured Ollama model, then
asks separately for:

- **Images:** Krea2 FP8, the Qwen3-VL 4B FP8 image text encoder, image VAE, the identity-edit,
  SNOFS LoRAs, plus the U2Net identity-background model.
  Approximately 22.2 GB on a fresh installation.
- **Video and voice:** the exact H3 hybrid, H3 text encoder and both VAEs, plus
  MOSS VoiceGenerator and Audio Tokenizer. Approximately 45.4 GB. Audio is part of
  this group because talking videos need the character voice samples.

Video generation also needs the image models to generate its starting image.
SNOFS Krea 1.4 downloads directly from the publisher's
[Ashen3/SNOFS repository](https://huggingface.co/Ashen3/SNOFS) into
`models/loras/snofs_krea_v1_4.safetensors`. Its separate publisher license applies;
the weights are not bundled in CastWorkshop's ZIP or Git repository. No workflow
or sampler settings are changed by setup.

The source URLs, exact revisions, sizes and SHA256 hashes are pinned in
`config/setup_dependencies.json`. Models download directly from Hugging Face or
GitHub; CastWorkshop does not redistribute weights or apply its Apache license
to third-party models. Setup displays publisher links before asking for agreement
to download under those publishers' terms. Some community repositories do not
provide explicit license metadata; consult their publisher pages for permitted use.

### Publisher terms and service use

The application source's Apache-2.0 license does not grant rights to its models.
Publisher metadata currently identifies [Krea2](https://huggingface.co/Comfy-Org/Krea-2)
and [MiniMax H3](https://huggingface.co/lilcheaty/MiniMax-H3-NVFP4) under their respective
community licenses, and [MOSS VoiceGenerator](https://huggingface.co/OpenMOSS-Team/MOSS-VoiceGenerator)
as Apache-2.0. Review the exact downloaded revisions and upstream/base-model terms
before relying on those labels for a particular use.

[SNOFS](https://huggingface.co/Ashen3/SNOFS) publishes a separate personal-use model
license. Its stated restrictions include weight redistribution, derivative models,
and running public/commercial generation services without a separate license.
Do not interpret CastWorkshop's LAN or Telegram support as permission to operate
such a service. Confirm applicable terms with the publisher before offering hosted
access. Output permissions and service permissions are distinct.

Custom nodes, voice dependencies, and recognition models also retain their own
licenses. Publisher metadata is not a legal clearance; the release owner must review
the pinned third-party terms, attribution obligations, and intended deployment.

For gated Hugging Face files, set `HF_TOKEN` in your environment after accepting the
publisher's access conditions. Tokens are not printed or written into manifests.
Authentication failures produce manual download links, and other files continue.

Setup asks the running ComfyUI server for its effective model folders, including
extra paths and shared Desktop folders. It checks each applicable category and its
subfolders before downloading. Existing models are trusted by exact filename and
size within 0.1%, capped at 1 MiB; no model hashing is performed. Repository support
files such as MOSS configs must also be in the correct repository subfolder.
Missing models download to ComfyUI's first reported folder for their category;
`COMFY_MODELS_DIR` or `--models-dir` overrides the download destination while still
checking all reported folders. If the server cannot report paths, setup warns and
checks the configured local folders instead. The server's paths must be accessible
from the computer running setup.

Downloads show progress, retain `.part` files after interruption, resume on rerun,
and check the expected transfer size before publishing. Model downloads are not
hashed; custom-node archives still receive SHA256 verification. A different existing
file is reported and preserved; setup never replaces it silently.
Provide enough disk space for approximately 67.6 GB of models plus ComfyUI, nodes,
Python dependencies and your generated media.

## Custom nodes and ComfyUI Python

Setup installs pinned archives of:

- [RES4LYF](https://github.com/ClownsharkBatwing/RES4LYF) for image sampling.
- [ComfyUI-Unload-Models](https://github.com/willblaschko/ComfyUI-Unload-Models)
  for images and video/voice.
- [TTS Audio Suite](https://github.com/diodiogod/TTS-Audio-Suite) for MOSS voice
  synthesis. Only the dependencies needed for the MOSS path are requested;
  the suite's full installer and unrelated voice-engine installations are not run.

Other workflow nodes, including the H3 nodes, Qwen image editing, resolution and
switching nodes, are supplied by recent ComfyUI versions. Setup checks registered
node types through `/object_info`; missing built-in nodes need a ComfyUI update
using its own updater or installation procedure. Setup does not replace ComfyUI.

Node dependencies are installed with **ComfyUI's Python**, never the CastWorkshop
environment. Setup detects `.venv`, portable `python_embeded` and Desktop Python
locations, or asks for an executable if detection fails. `COMFY_PYTHON` can also
provide it. ComfyUI must already have its appropriate Torch and torchaudio builds
and a working `pip`. A dry-run dependency resolver protects existing Torch,
torchaudio, torchvision, NumPy, Pillow, Triton and ComfyUI packages. If the requested
dependencies conflict, setup reports the conflict without installing that package's
dependencies. It does not force a core-package upgrade or invoke vendor `install.py`.

The image group also installs ONNX Runtime and OpenCV into ComfyUI's Python for
`scripts/isolate_identity.py` and downloads the U2Net human-segmentation
model into **CastWorkshop's** `models/u2net_human_seg.onnx`, or your configured
`IDENTITY_SEGMENTATION_MODEL`. This is needed by scene-anchored two-character media.
The video group installs `imageio-ffmpeg` and locates its bundled FFmpeg binary for
continuation frame extraction. On platforms without a supported binary, setup
reports that FFmpeg still needs manual installation.

Detected ComfyUI Python and FFmpeg paths are saved in `.env` when the corresponding
settings are blank; existing explicit settings and tokens are preserved.

Existing custom-node directories are preserved. Setup reruns dependency installation
for packages it previously installed at the pinned revision; use ComfyUI Manager
for independently installed packages. **Restart ComfyUI afterward** to load new
nodes. Setup never stops ComfyUI or interrupts a generation.

## Rerun only model/node setup

From the CastWorkshop root:

```text
python scripts/comfy_setup.py --list
python scripts/comfy_setup.py --images --comfyui-path "path/to/ComfyUI"
python scripts/comfy_setup.py --video --comfyui-path "path/to/ComfyUI"
python scripts/comfy_setup.py --images --video --check-only
```

`--images` and `--video` explicitly select downloads under the displayed publisher
terms. They can be combined. `--check-only` checks node registration without
installing, downloading or generating anything. It returns a failure status if
ComfyUI is unreachable or required nodes are missing.

Use `--comfy-python "path/to/python"` for an environment not discovered automatically.
Missing models default to the running ComfyUI's first folder for their category,
or the selected installation's `models` directory when the server is unavailable.
To override the download destination, pass `--models-dir "path/to/shared/models"` or set `COMFY_MODELS_DIR` before
running the regular setup. That library must already be configured in ComfyUI's
`extra_model_paths.yaml`; setup does not rewrite that file. Category directory
junctions/symlinks are respected. Select the local ComfyUI installation actually
used by the configured server; a remote server cannot load models downloaded only
to the client machine.

The sanitized model readiness report remains available in web Settings. MOSS's
folder-based inventory is reported as unverified there; setup checks its individual
downloaded files, while registered nodes are checked separately.
