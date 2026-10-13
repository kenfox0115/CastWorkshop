# CastWorkshop image defaults

`worker/workflows/krea2_identity_edit_clownshark.json` is the single runtime
source for native identity images in every mode and aspect. It is a sanitized
copy of the approved Downloads workflow: only its example prompt, reference
filenames, and seed were cleared. Model and sampling settings remain supplied
values, rather than being reconstructed from the legacy presets.

The approved graph uses Krea2 FP8, the Qwen3-VL 4B FP8 encoder, the Qwen image
VAE, identity-edit LoRA strength 1, and ClownsharKSampler_Beta at node 112:
8 steps, CFG 1, exponential/ddim, beta57, eta 0.5, denoise 1,
steps_to_run -1, standard mode, and bongmath false. Both reference boosts are
1.5. Positive grounding is 512; negative grounding remains 768 as supplied.

Runtime changes only prompt, reference files, random seed, aspect ratio, and
SNOFS strength. Vertical is 9:16 and cinematic is 16:9. SFW disables SNOFS;
adult mode enables it. The builder inserts model unloading after VAE decode
and before image saving, preserving the Ollama/ComfyUI memory boundary.

All four former native preset names, including queued jobs and saved overrides,
resolve to the approved canonical template. Their archived files are no longer
runtime defaults. Look candidates keep their separate text-to-image workflow.
Single-character scene insertion and both sequential two-character scene passes
use this same canonical graph. Scene-attached two-character videos also use
these two insertion passes for their opening image, then pass the final image
to H3. Video continuations use the source video's ending frame instead.

The supplied FP8 diffusion model must be installed as
`krea2_turbo_fp8_scaled.safetensors`; the model manifest checks its presence.
It was confirmed available in the local ComfyUI inventory. Setup installs FP8 with its pinned publisher revision, exact size, and SHA256;
Look candidates and legacy image presets use this same FP8 checkpoint and Qwen3-VL
4B FP8 encoder. Setup no longer downloads the INT8 checkpoint or separate Huihui
image encoder. The FP8 size and hash were
verified from the [publisher pointer](https://huggingface.co/Comfy-Org/Krea-2/raw/main/diffusion_models/krea2_turbo_fp8_scaled.safetensors). Both Krea2 identity-edit and RES4LYF custom nodes are
required; setup already registers them.

Prompt construction normalizes supported wardrobe edits to completed final
states, resolves collectively requested or individually named outfits, and
prevents conflicting source clothing from being preserved. Adult mode declares
adulthood, and two-character output explicitly requires exactly two distinct
identities without blending or duplication. Unchanged outfits and negated
wardrobe requests remain unchanged. This deterministic normalization handles
common scene fragments and explicit wardrobe edits; it does not invent scene
content or use an extra model call.
