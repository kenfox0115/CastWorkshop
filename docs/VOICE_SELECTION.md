# Selected voice references

Voice candidate generation and audition recordings are unchanged. Selecting a
candidate downloads that exact output recording (including its output subfolder),
locates the start of "Soft morning light moved across" using CPU word timestamps,
and trims off the introduction, including "It's really nice to meet you."
The remainder is uploaded as a WAV voice reference. No second MOSS synthesis job
is submitted during selection. The original candidate is retained.

Run `python setup_project.py` to install the worker dependencies, verify FFmpeg,
and cache faster-whisper's CPU `base.en` model in ignored `logs/voice-trim-models`.
Setup uses the application's virtual environment and reports if preparation fails.
It requires network access for the initial dependency/model downloads; subsequent
voice selections reuse the cached model. Configured/system FFmpeg is preferred,
with the worker's `imageio-ffmpeg` binary as a fallback. No external ComfyUI
installation changes are required. Manual requirements installation also works,
but the model is then downloaded on first use unless prepared in advance with
`python -m worker.voice_trim --prepare` in the worker environment.

The passage matcher tolerates one recognized-word substitution, omission, or insertion
after the distinctive opening word. If the boundary is missing, repeated, or overlaps
the preceding word, selection fails rather than guessing a cut or generating a replacement voice.
Existing saved character references are not retroactively modified.

Speech recognition timestamps are estimates. A live audition of trimmed samples
is required to assess boundary accuracy for actual MOSS voices; offline tests
verify boundary handling and that promotion never invokes synthesis. When FFmpeg
is available, an additional test verifies the retained PCM samples exactly.
