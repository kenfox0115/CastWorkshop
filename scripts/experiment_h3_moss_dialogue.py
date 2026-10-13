"""Run an isolated H3 control vs MOSS exact-dialogue experiment.

This script never changes application settings or the production job path. It writes
generated media and a machine-readable manifest below output/h3_moss_experiment/.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import sys
import time
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from worker import comfy_client  # noqa: E402
from worker.video_contract import VideoRequest  # noqa: E402
from scripts.voice_reference import VOICE_REFERENCE_PASSAGE  # noqa: E402
from vps.artifacts import resolve_artifact  # noqa: E402


DEFAULT_LINE = "aww, which one is yours"
DEFAULT_SCENE = (
    'Frankie points toward the dogs in the park, then says '
    '"aww, which one is yours" in a sweet, excited tone. '
    "She stops speaking while the quiet park scene continues naturally."
)
WORKFLOW = "presets/minimax_h3_2char_ref2v_widescreen.json"


def extract_video_continuation_frame(relative_path: str, offset_seconds: float = 0.15) -> bytes:
    """Extract the same near-final composition frame used by the production continuation path."""
    source = resolve_artifact(relative_path)
    executable = shutil.which(os.environ.get("FFMPEG_PATH", "ffmpeg"))
    if not source.is_file() or not executable:
        raise RuntimeError("The source video or FFmpeg is unavailable")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    detail = ""
    for offset in (max(0.05, float(offset_seconds)), 0.5, 1.0):
        completed = subprocess.run(
            [executable, "-hide_banner", "-loglevel", "error", "-sseof", f"-{offset:.3f}",
             "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-f", "image2pipe",
             "-vcodec", "png", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
            creationflags=flags,
        )
        if completed.returncode == 0 and completed.stdout.startswith(b"\x89PNG"):
            return completed.stdout
        detail = completed.stderr.decode("utf-8", "replace")[-500:]
    raise RuntimeError(f"Could not extract the source composition: {detail}")


def character(name: str) -> dict:
    with sqlite3.connect(ROOT / "vps" / "jobs.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, name, base_image_path, voice_sample_path FROM characters "
            "WHERE lower(name)=lower(?)", (name,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"Character {name!r} was not found in vps/jobs.db")
    result = dict(row)
    if not result.get("base_image_path") or not result.get("voice_sample_path"):
        raise RuntimeError(f"Character {name!r} lacks an image or saved voice")
    return result


def retained_voice_description(character_id: int) -> str:
    """Recover the source-owned acoustic description without relying on Comfy history."""
    with sqlite3.connect(ROOT / "vps" / "jobs.db") as conn:
        rows = conn.execute(
            "SELECT payload FROM jobs WHERE job_type='voice_sample_batch' ORDER BY id DESC"
        ).fetchall()
    for (raw_payload,) in rows:
        payload = json.loads(raw_payload or "{}")
        if payload.get("character_id") == character_id and payload.get("voice_description"):
            return payload["voice_description"]
    raise RuntimeError("The character's retained MOSS voice description was not found")


def audio_duration(path: Path) -> float | None:
    """Measure WAV directly or use ffprobe for ComfyUI's usual FLAC output."""
    if not path.is_file():
        return None
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / float(audio.getframerate())
    ffprobe = shutil.which(os.environ.get("FFPROBE_PATH", "ffprobe"))
    if not ffprobe:
        return None
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        return round(float(completed.stdout.strip()), 3)
    except ValueError:
        return None


def generate_moss(text: str, voice_instruction: str, seed: int,
                  label: str, output_dir: Path) -> tuple[str, dict]:
    """Generate one matched experimental MOSS clip and promote it to /input."""
    with open(comfy_client.resolve_workflow_path(comfy_client.MOSS_WORKFLOW_FILE),
              encoding="utf-8") as handle:
        workflow = copy.deepcopy(json.load(handle))
    designer = workflow[comfy_client.MOSS_VOICE_DESIGN_NODE]["inputs"]
    designer["reference_text"] = text
    designer["voice_instruction"] = voice_instruction
    designer["seed"] = seed
    workflow[comfy_client.MOSS_SAVE_NODE]["inputs"]["filename_prefix"] = (
        f"audio/h3_moss_experiment_{label}_{random.randint(0, 99999999):08d}"
    )
    started = time.monotonic()
    prompt_id = comfy_client.submit_prompt(workflow)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        response = comfy_client.requests.get(
            f"{comfy_client.COMFY_URL}/history/{prompt_id}", timeout=10
        )
        response.raise_for_status()
        entry = response.json().get(prompt_id)
        if not entry:
            time.sleep(2)
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            raise RuntimeError(f"MOSS job {prompt_id} failed: {status.get('messages')}")
        outputs = entry.get("outputs", {}).get(comfy_client.MOSS_SAVE_NODE, {}).get("audio", [])
        if outputs:
            source = outputs[0]
            promoted = comfy_client.promote_output_to_input(
                source["filename"], subfolder=source.get("subfolder", "")
            )
            suffix = Path(source["filename"]).suffix or ".flac"
            saved = output_dir / f"MOSS_{label}{suffix}"
            saved.write_bytes(comfy_client.fetch_image_bytes(source))
            comfy_client.free_vram()
            return promoted, {
                "prompt_id": prompt_id,
                "reference_text": text,
                "voice_instruction": voice_instruction,
                "seed": seed,
                "generation_seconds": round(time.monotonic() - started, 2),
                "duration_seconds": audio_duration(saved),
                "input_filename": promoted,
                "output": str(saved.resolve()),
            }
        time.sleep(2)
    raise TimeoutError(f"MOSS job {prompt_id} did not finish within 5 minutes")


def two_character_prompt(name_a: str, name_b: str, speaker: str, line: str,
                         scene: str, exact_audio: bool) -> str:
    visual_timeline = VideoRequest.create(scene, speaker, line).visual()
    speaker_subject = 1 if speaker == name_a else 2
    audio_role = (
        "completed dialogue performance for the target video"
        if exact_audio else "voice-timbre reference; its recorded words are excluded from the target soundtrack"
    )
    audio_retention = (
        "partially_copy - directly preserve its complete spoken line, words, language, voice, delivery, "
        "timing, and clean speech signal; add only non-vocal environmental ambience around it."
        if exact_audio else "reference - voice timbre only; its recorded words are not copied."
    )
    speech_instruction = (
        f"<Subject {speaker_subject}> (S1), {speaker}, performs the completed dialogue from <Audio 1>: "
        f"<d>[English] {line}</d>."
        if exact_audio else
        f"<Subject {speaker_subject}> (S1), {speaker}, speaks using the voice timbre referenced from "
        f"<Audio 1>: <d>[English] {line}</d>."
    )
    return (
        "subject_definitions:\n"
        f"<Subject 1> is {name_a}, whose visual identity comes from <Picture 1>.\n"
        f"<Subject 2> is {name_b}, whose visual identity comes from <Picture 2>.\n"
        "<Picture 3> is the target composition and opening-scene reference containing both subjects.\n"
        f"<Audio 1> is {speaker}'s {audio_role}.\n\n"
        "summary:\n"
        "[reference generation + audio reference] Preserve both character identities and begin from the "
        "supplied two-person composition while generating the requested performance and park ambience.\n\n"
        "retention_analysis:\n"
        f"<Subject 1> (appears in [Shot 1]): fully_preserved - {name_a}'s identity remains consistent with <Picture 1>.\n"
        f"<Subject 2> (appears in [Shot 1]): fully_preserved - {name_b}'s identity remains consistent with <Picture 2>.\n"
        "<Picture 3> ([Shot 1] composition anchor): fully_preserved - both subjects' positions, framing, "
        "clothing, park setting, lighting, and props establish the opening state.\n"
        f"<Audio 1>: {audio_retention}\n\n"
        "detailed_description:\n"
        "The target video is live-action and cinematic. [Shot 1] Begin exactly from <Picture 3>, with "
        f"{name_a} and {name_b} both visible in their established positions. Follow this authored timeline "
        f"in order: {visual_timeline} {speech_instruction} Lip movement starts with the first audible word and ends "
        "with the last. The other subject listens without speaking. "
        "Before and after that supplied line, the mouth remains closed and no additional human voice, "
        "partial word, filler, laughter, sigh, mutter, or vocalization occurs. Continue naturally in one "
        "uninterrupted shot with no camera cut.\n\n"
        "overall_soundscape: Quiet outdoor park ambience, distant dogs, birds, and a light breeze around "
        "the directly reused dialogue; no competing human speech and no modification of the supplied voice.\n\n"
        "non_diegetic_music: N/A"
    )


def render(reference_a: str, reference_b: str, scene_image: str, audio: str, prompt: str, seed: int,
           duration: float, output_path: Path) -> dict:
    with open(comfy_client.resolve_workflow_path(WORKFLOW), encoding="utf-8") as handle:
        workflow = copy.deepcopy(json.load(handle))
    workflow[comfy_client.REF2V_IMAGE_NODE_ID]["inputs"]["image"] = reference_a
    workflow[comfy_client.TWOCHAR_REF_IMAGE_B]["inputs"]["image"] = reference_b
    workflow[comfy_client.TWOCHAR_REF_SCENE]["inputs"]["image"] = scene_image
    workflow[comfy_client.REF2V_AUDIO_NODE_ID]["inputs"]["audio"] = audio
    workflow["136"]["inputs"].pop("ref_audios.ref_audio_1", None)
    workflow.pop(comfy_client.TWOCHAR_REF_AUDIO_B, None)
    workflow[comfy_client.REF2V_PROMPT_NODE_ID]["inputs"]["value"] = prompt
    workflow[comfy_client.REF2V_DURATION_NODE_ID]["inputs"]["value"] = duration
    workflow[comfy_client.REF2V_SEED_NODE_ID]["inputs"]["noise_seed"] = seed
    started = time.monotonic()
    prompt_id = comfy_client.submit_prompt(workflow)
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        response = comfy_client.requests.get(
            f"{comfy_client.COMFY_URL}/history/{prompt_id}", timeout=10
        )
        response.raise_for_status()
        entry = response.json().get(prompt_id)
        if not entry:
            time.sleep(5)
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            raise RuntimeError(f"H3 job {prompt_id} failed: {status.get('messages')}")
        outputs = entry.get("outputs", {}).get(comfy_client.REF2V_SAVE_NODE_ID, {})
        info = next((outputs[key][0] for key in comfy_client.VIDEO_OUTPUT_KEYS if outputs.get(key)), None)
        if info:
            output_path.write_bytes(comfy_client.fetch_image_bytes(info))
            comfy_client.free_vram()
            return {
                "prompt_id": prompt_id,
                "seed": seed,
                "render_seconds": round(time.monotonic() - started, 2),
                "output": str(output_path.resolve()),
                "comfy_output": info,
            }
        time.sleep(5)
    raise TimeoutError(f"H3 job {prompt_id} did not finish within 30 minutes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character-a", default="John")
    parser.add_argument("--character-b", default="Frankie")
    parser.add_argument("--speaker", default="Frankie")
    parser.add_argument("--source-artifact", type=int, default=38)
    parser.add_argument("--reuse-run", help="Prior experiment directory whose MOSS audio should be reused")
    parser.add_argument("--line", default=DEFAULT_LINE)
    parser.add_argument("--scene", default=DEFAULT_SCENE)
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--voice-seed", type=int)
    args = parser.parse_args()

    info_a = character(args.character_a)
    info_b = character(args.character_b)
    if args.speaker not in (info_a["name"],info_b["name"]):
        raise RuntimeError("Speaker must match character A or character B")
    seed = args.seed if args.seed is not None else random.randint(0, 2**31 - 1)
    voice_seed = (args.voice_seed if args.voice_seed is not None
                  else random.randint(0, 2**63 - 1))
    speaker_info = info_a if args.speaker == info_a["name"] else info_b
    voice_instruction = retained_voice_description(speaker_info["id"])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_dir = ROOT / "output" / "h3_moss_experiment" / stamp
    output_dir.mkdir(parents=True, exist_ok=False)
    control_prompt = two_character_prompt(info_a["name"],info_b["name"],args.speaker,args.line,args.scene,False)
    partial_prompt = two_character_prompt(info_a["name"],info_b["name"],args.speaker,args.line,args.scene,True)
    with sqlite3.connect(ROOT / "vps" / "jobs.db") as conn:
        source = conn.execute("SELECT relative_path FROM artifacts WHERE id=? AND kind='video'",(args.source_artifact,)).fetchone()
    if not source:
        raise RuntimeError(f"Video artifact {args.source_artifact} was not found")
    scene_image = comfy_client.upload_bytes_to_input(
        extract_video_continuation_frame(source[0]), "h3_moss_two_character_composition.png"
    )
    manifest = {
        "experiment_name": "H3 production control vs exact MOSS dialogue partially_copy",
        "characters": [info_a["name"],info_b["name"]],
        "speaker": args.speaker,
        "reference_images": [info_a["base_image_path"],info_b["base_image_path"]],
        "composition_source_artifact_id": args.source_artifact,
        "composition_image": scene_image,
        "production_voice_reference": speaker_info["voice_sample_path"],
        "line": args.line,
        "scene": args.scene,
        "duration_seconds": args.duration,
        "workflow": WORKFLOW,
        "seed": seed,
        "voice_seed": voice_seed,
        "voice_instruction": voice_instruction,
        "control": {"prompt": control_prompt},
        "experiment": {"prompt": partial_prompt},
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"[experiment] output: {output_dir}", flush=True)
    if args.reuse_run:
        prior=json.loads((Path(args.reuse_run)/"manifest.json").read_text(encoding="utf-8"))
        neutral_audio=prior["control"]["moss"]["input_filename"]
        exact_audio=prior["experiment"]["moss"]["input_filename"]
        manifest["control"]["moss"]={**prior["control"]["moss"],"reused":True}
        manifest["experiment"]["moss"]={**prior["experiment"]["moss"],"reused":True}
        print(f"[experiment] reusing MOSS audio from {args.reuse_run}",flush=True)
    else:
        print("[experiment] generating matched neutral MOSS reference...", flush=True)
        neutral_audio, neutral_meta = generate_moss(
            VOICE_REFERENCE_PASSAGE, voice_instruction, voice_seed, "neutral_reference", output_dir
        )
        manifest["control"]["moss"] = neutral_meta
        print("[experiment] generating matched exact MOSS dialogue...", flush=True)
        exact_audio, exact_meta = generate_moss(
            args.line, voice_instruction, voice_seed, "exact_dialogue", output_dir
        )
        manifest["experiment"]["moss"] = exact_meta
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("[experiment] rendering production control...", flush=True)
    manifest["control"].update(render(
        info_a["base_image_path"], info_b["base_image_path"], scene_image,
        neutral_audio, control_prompt,
        seed, args.duration, output_dir / "A_control.mp4",
    ))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("[experiment] rendering MOSS partially_copy variant...", flush=True)
    manifest["experiment"].update(render(
        info_a["base_image_path"], info_b["base_image_path"], scene_image,
        exact_audio, partial_prompt,
        seed, args.duration, output_dir / "B_moss_partially_copy.mp4",
    ))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[experiment] complete: {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
