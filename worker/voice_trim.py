"""Trim candidate introductions without synthesizing another voice."""
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def resolve_ffmpeg():
    """Honor configured/system FFmpeg, then use the worker's bundled binary."""
    configured = os.environ.get("FFMPEG_PATH", "ffmpeg") or "ffmpeg"
    executable = shutil.which(configured)
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(executable).is_file():
            return executable
    except (ImportError, RuntimeError, OSError):
        pass
    raise RuntimeError("Voice trimming requires FFmpeg; rerun setup or set FFMPEG_PATH")


def load_recognizer():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Voice trimming requires faster-whisper from worker/requirements.txt") from exc
    cache = Path(__file__).resolve().parents[1] / "logs" / "voice-trim-models"
    return WhisperModel("base.en", device="cpu", compute_type="int8", download_root=str(cache))


def prepare():
    """Fetch/cache recognition weights during setup and validate the audio tool."""
    executable = resolve_ffmpeg()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run([executable, "-version"], check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=30, creationflags=flags)
    model = load_recognizer()
    del model
    print("[setup] Voice trimming ready: CPU base.en model cached and FFmpeg verified")


def _edit_distance(left, right):
    """Return token-level Levenshtein distance for two short word sequences."""
    previous = list(range(len(right) + 1))
    for row, left_word in enumerate(left, 1):
        current = [row]
        for column, right_word in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_word != right_word),
            ))
        previous = current
    return previous[-1]


def reference_start(words):
    """Find the known passage boundary despite one local ASR word error."""
    normalized = [re.sub(r"[^a-z]", "", word.word.lower()) for word in words]
    anchor = ["soft", "morning", "light", "moved", "across"]
    # Word recognition can substitute, omit, or insert one word in this fixed
    # passage. Anchor on its distinctive first word and tolerate one edit in
    # the rest; never use duration as a fallback that could cut into the audio.
    matches = {}
    for index, word in enumerate(normalized):
        if word != anchor[0]:
            continue
        for span in range(len(anchor) - 1, len(anchor) + 2):
            tail = normalized[index + 1:index + span]
            if len(tail) != span - 1:
                continue
            distance = _edit_distance(anchor[1:], tail)
            if distance <= 1:
                matches[index] = min(matches.get(index, distance), distance)
                break
    if len(matches) != 1:
        raise RuntimeError("Cannot confidently locate the voice introduction boundary")
    index = next(iter(matches))
    start = float(words[index].start)
    if index == 0 or start <= 0:
        raise RuntimeError("Voice candidate has no recognizable introduction to trim")
    previous_end = float(words[index - 1].end)
    if previous_end > start:
        raise RuntimeError("Voice introduction overlaps the reference passage")
    # Preserve up to 40ms of the pause before the first retained word.
    return max(previous_end, start - 0.04)


def trim_candidate(audio_bytes):
    """Recognize on CPU and retain the original PCM samples after the greeting."""
    executable = resolve_ffmpeg()
    model = load_recognizer()
    segments, _ = model.transcribe(io.BytesIO(audio_bytes), language="en", word_timestamps=True)
    words = [word for segment in segments for word in (segment.words or [])]
    start = reference_start(words)
    del model
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="castworkshop-voice-") as temporary:
        source = Path(temporary) / "candidate.audio"
        source.write_bytes(audio_bytes)
        result = subprocess.run(
            [executable, "-hide_banner", "-loglevel", "error", "-i", str(source),
             "-af", f"atrim=start={start:.6f},asetpts=PTS-STARTPTS",
             "-c:a", "pcm_s32le", "-f", "wav", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, creationflags=flags,
        )
    if result.returncode or len(result.stdout) < 128:
        raise RuntimeError("Could not trim selected voice: " + result.stderr.decode("utf-8", "replace")[-500:])
    return result.stdout


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Prepare voice reference trimming")
    parser.add_argument("--prepare", action="store_true", required=True)
    parser.parse_args()
    prepare()
