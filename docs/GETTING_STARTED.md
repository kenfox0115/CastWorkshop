# Getting started

## Configure the local services

Install Ollama and pull the model you want to use for chat and media planning. The
default configured name is the locally created `qwen3-14b-64k` alias. Setup can install
it by downloading `huihui_ai/qwen3-abliterated:14b-v2-q8_0` and applying the bundled
Modelfile. Set `OLLAMA_MODEL` in `.env` or use the bot's `/model` command if your
installed model has a different name.

Install ComfyUI and start its API server. The application probes loopback port 8188
and nearby fallback ports unless `COMFY_URL` is set. Setup can optionally install
the required models and custom nodes, with separate image and video/voice groups.
Restart ComfyUI after installing nodes. The complete inventory, download behavior,
publisher terms and authentication instructions are in `MODELS.md`. Look candidates,
native identity images and scene insertion share Krea2 FP8 and its Qwen3-VL 4B
FP8 encoder in both content modes.

When Ollama, ComfyUI, and CastWorkshop run on the same computer, leave Ollama and
ComfyUI listening locally: `127.0.0.1:11434` and `127.0.0.1:8188`. Setup checks
whether their APIs respond; it does not start or reconfigure those separately
installed services. Ollama's desktop app normally starts its API service, while
ComfyUI Desktop requires **Start Server**. If a service is on a different computer,
configure that service's network access and point `OLLAMA_URL` or `COMFY_URL` to it.

## Configure CastWorkshop

Run:

```text
python setup_project.py
```

Setup appends console output, dependency installer output, and error tracebacks to
`logs/setup.log`. A failed interactive setup waits for Enter before closing. After
setup completes, you can choose to launch CastWorkshop immediately.

Setup asks whether to allow access from other devices. The default/recommended choice
is **This computer only**, saved as `API_HOST=127.0.0.1`. Choosing trusted-network
access saves `API_HOST=0.0.0.0` after displaying the no-login/HTTPS warning. Existing
explicit settings are preserved. Headless setup never prompts: it honors an explicit
valid `API_HOST` from the environment or `.env`, otherwise defaulting to localhost.

Setup asks separately whether to install image dependencies (about 22.2 GB) and
video plus MOSS voice dependencies (about 45.4 GB): approximately 67.6 GB combined
before reuse. Setup checks the running ComfyUI's model folders, including extra paths, and skips
exact filenames whose sizes match within 0.1%, capped at 1 MiB. Models are not hashed.
Allow additional space for ComfyUI, Python environments, temporary files and media.
The offline suite has run on Windows; beta GPU acceptance, minimum VRAM and
macOS/Linux GPU compatibility have not been verified. See [MODELS.md](MODELS.md)
for the full inventory.
SNOFS Krea 1.4 is included in the image group.

Or create `.env` manually from `.env.example`. Keep `.env` private. Telegram is
optional; leave its token blank for web-only use. Do not run two copies with the
same token because Telegram permits only one long-polling owner.

Useful optional settings:

- `OLLAMA_URL` and `COMFY_URL`: local service endpoints
- `OLLAMA_MODEL`: initial model name when the database has no saved selection
- `OLLAMA_TIMEOUT`: LLM request timeout in seconds
- `COMFYUI_PATH`, `COMFY_PYTHON`: ComfyUI root and Python environment for node setup
- `COMFY_MODELS_DIR`: shared model destination used by setup (already configured in ComfyUI)
- `API_HOST`: listener address (`127.0.0.1` by default; `0.0.0.0` for trusted-network access)
- `API_PORT` and `VPS_URL`: queue port and internal worker endpoint (normally loopback)
- `PEAK_HOURS_GATE=1`: defer heavy work during the configured evening window
- `VIDEO_START_DELAY_SECONDS`: pause between scene-image and video generation (default 5)
- `TELEGRAM_ENABLED=0`: run locally without polling Telegram
- `WORKER_ENABLED=0`: run the queue service without a worker

## Start and inspect

Start Ollama and ComfyUI first, then run `python start.py`. The launcher automatically
uses the `.venv` interpreter populated by setup for its supervisor and worker, even
if the command was started with a system Python. The launcher returns after
the local API reports ready and opens the local web interface on an interactive desktop.
Set `OPEN_WEB_UI=0` in `.env` for a headless launch. Check the stack with:

```text
python scripts/inspect_env.py
```

The expected result is a running supervisor with VPS and worker children, plus HTTP 200
from the VPS, ComfyUI, and Ollama. Open the local dashboard at
`http://127.0.0.1:8787/web`. Select a character there to chat or request a single-character image or video. Browser media is stored under `data/media/` and remains available in web conversation history. Use `python stop.py` before moving the folder or
changing its Telegram token.

The server honors `API_HOST` and defaults to `127.0.0.1`. To allow other devices on a
trusted network, choose LAN access during setup or set `API_HOST=0.0.0.0` in `.env`
and restart. `start.py` prints a usable LAN URL when it can detect one and LAN mode
is enabled; local-only mode prints only the loopback URL. Open the LAN URL on the
other device. CastWorkshop has no built-in login or HTTPS: anyone who can reach this
computer may access its interface and generated content. Use LAN mode only on a
trusted home/LAN/Tailscale network; do not expose port 8787 directly to the public
internet. An operating-system
firewall may ask you to allow Python on private networks the first time.
Ollama and ComfyUI do not need network listen mode for this: the browser talks to
CastWorkshop, which calls both local services itself.

## First use in the browser

Telegram is not required. After configuring the local services and dependencies:

1. Run `python start.py` from the CastWorkshop folder.
2. Open `http://127.0.0.1:8787/web` in your browser.
3. Select a character from the cast, or choose **+ New**. Fill in **Name**, **Look**
   and **Personality**, then choose **Create character**. Creation generates a
   profile, reference-look candidates and voice candidates. Select a look, listen
   to the voice samples, and use **Choose voice** with the candidate number to
   save a voice. Select the character marked Setup to resume unfinished creation.
4. With a character selected, choose **Chat**, type a message and press **Send**.
5. For an image, choose **Image**, describe the scene or requested change, and
   press **Send**. Results appear in the conversation.
6. Voice samples are generated during character creation. While choosing a voice,
   use **Reroll all voices** to generate another set, then listen and select one.
7. For a video, choose **Video**, describe the action and any dialogue, and press
   **Send**. Keep the character's saved reference image and voice ready. Completed
   videos appear in the conversation; **Continue video** starts a follow-up clip.

New installations begin in SFW mode. Generation needs the corresponding models
and nodes; long media jobs show their progress in the interface. Offline test
results do not guarantee image, speech or motion quality.

### Two-character media

After basic single-character use, create a second character. Both need reference
images for **Two-character image**; both also need saved voices for
**Two-character video**. Choose two distinct characters and describe their scene.
For an image, choose **Generate image**. For a video, put spoken lines in quotes
and name their speakers, choose **Review speakers**, check the assignments, then
choose **Confirm and generate**.

### Optional Telegram

Configure a BotFather token during setup or in web **Settings** under **Telegram**.
In Settings, supply **Bot token**, enable **Enable Telegram bot**, wait for the
saved confirmation, and restart CastWorkshop to apply the change. Leave Telegram
disabled for browser-only use; never run two copies polling the same token.

Message the bot and use `/newcharacter` to create a character. `/image` and `/video`
generate media for the active character; `/multiimage` and `/multivideo` select
two saved characters. `/mode`, `/videos` and `/aspect` change the corresponding
settings. `/debug` shows or hides advanced commands such as `/model`, `/workflow`,
`/endpoints` and `/status`.

## Troubleshooting

- No Telegram response: inspect `logs/vps.log`, confirm the token, and confirm no other
  copy owns the same token.
- VPS unavailable: inspect `logs/supervisor.log` and `logs/vps.log`.
- Media job failed: inspect `logs/worker.log` and the matching ComfyUI history entry.
- A completed render was not delivered: keep the generated file in place and inspect
  the delivery outbox state; recovery is designed to retry durable deliveries.
- Stale heartbeat with a live PID: collect `python scripts/recovery_report.py` before
  restarting so the exit evidence is preserved.
- ComfyUI stopped: restart ComfyUI itself. The application supervisor does not own it.
- GPU out of memory: close other GPU workloads and select model quantizations that fit.

Use `python scripts/clear_stuck_jobs.py` only after reading its help and preserving a
database backup. Routine recovery should be allowed to run first.

## Verify the 0.9.0-beta.1 source candidate

Run the verifier using the environment populated by setup:

```text
# Windows
.venv\Scripts\python.exe scripts/verify.py
# macOS / Linux
.venv/bin/python scripts/verify.py
```

The verifier needs the application Python dependencies, but uses temporary databases
and local fake services for integration tests. It does not submit real media jobs.

### Restart a failed character setup step

After a look or voice setup failure, use **Restart look selection** or
**Restart voice selection** in the failed creation dialog or job bar. This
creates fresh candidates from the original generation request and returns to
selection, preserving the character profile and saved assets. **Retry** still
reruns only the failed operation, such as storing the selected recording.

In Telegram, use `/retry <job-id> step`, or `/retry step` for the latest failed
job. Finish current generation or setup before restarting another step.
