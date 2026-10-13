# Image prompt construction

`worker/image_prompts.py` is the shared, pure builder. VPS and web enqueue clean
instructions; the Comfy client reassembles native single-character edits against
the actual graph roles. Assembly is idempotent. Known historical identity headers
are unwrapped for jobs queued before the change.

Reference roles follow the graph, not names inferred from prompt prose:

| Path | image 1 | image 2 | image 3 |
| --- | --- | --- | --- |
| Single identity edit | Identity | — | — |
| Scene + identity | Environment | Identity | — |
| Two identities | Character A | Character B | — |
| Two identities + scene image: Pass 1 | Environment | Character A | — |
| Two identities + scene image: Pass 2 | Pass 1 scene including A | Character B | — |
| Two identities + scene video opening: Pass 1 | Environment | Character A | — |
| Two identities + scene video opening: Pass 2 | Pass 1 scene including A | Character B | — |

The sections are scene preservation, named identity preservation, requested edit,
and optional look expression. Requested style/medium stays in the authored edit;
ordinary edits do not receive an invented style. The character concept is not
copied into edit instructions, so old outfits, poses and identity backgrounds do
not compete with the new request. Explicit edits take precedence over preservation.

Before: the scene job called image 1 the character and image 2 the environment;
the compositor then prepended the opposite mapping. Both also repeated identity
rules and the entire character concept.

After, for a green-dress edit:

> Use image 1 for the environment, layout, camera angle, lighting, furnishings and
> props, except changes explicitly requested below. Use image 2 for Ava's facial
> identity, hair, skin tone, ethnicity and build, except explicitly requested
> changes. Clothing, pose and background follow the request, not the identity
> image. Requested edit: Wear a green dress and sit by the window.

Look cues use saved personality first, then concept/archetype words. One facial
mood and at most one complementary posture keep mixed personalities restrained.
Explicit expression directions suppress automatic cues. Sparse descriptions get
attentive eyes and a relaxed natural mouth. Photographic and explicitly stylized
candidates use the same restrained expression rules. Candidate clothing policy,
workflow settings, sampling and VRAM handoff remain unchanged.

Validation rejects duplicate role assignments, explicit reversed role clauses,
and clear simultaneous clothed/fully-unclothed states for a single subject. This
is intentionally limited validation, not a general natural-language contradiction
solver. Multi-person differing outfits and explicit clothing transitions remain
valid. Model-generated descriptions can still require review.

## Manual QA after restarting the stack

1. Compare web and Telegram single-character edits with and without a scene.
2. Change the outfit and pose; check that the old outfit/background do not bleed in.
3. Test two-character images, including sequential insertion into an active scene.
4. Check video starting images for correct scene, identity and pre-action state.
5. Create/reroll confident, shy, playful and introspective looks; compare sparse
   personality and explicitly neutral-expression cases. Include a stylized concept.
6. Exercise vertical/cinematic and SFW/NSFW settings. Confirm requested visual
   changes are represented without unexpected clothing-preservation instructions.

Offline tests verify assembly and workflow bindings; they cannot establish visual
quality or guarantee a generative model obeys every instruction.

## Sequential two-character scene images

Both `multi_character_image` and `multi_character_video` jobs with a scene
reference select the sequential path. Video image passes use the validated
opening-frame scene, excluding dialogue and later actions. Continuations extract
the original ending frame; two-character media without a scene keeps its prior
composition path. Both passes use the canonical native identity-edit graph;
no workflow JSON is duplicated or modified. Explicit image overrides remain
applicable to jobs without a scene; sequential scene jobs use the known-good
native baseline and log that choice.

Pass 1 calls `generate_character_scene_image` with a preassembled staging prompt:
show the first character only, positioned for the requested final interaction,
and leave room for the second. The optional `assembled_prompt` argument preserves
this named staging instruction; ordinary single-character callers retain their
existing behavior. `participant_count` lets prompt validation distinguish two
characters' different clothing states from a conflicting single-person outfit.

Pass 2 adds the second identity to the existing scene and protects the first
character's appearance. Requested interactions may adjust the first character's
pose. It does not impose a side-by-side arrangement when the request calls for
another composition, such as sitting across a table.

The existing HTTP upload helper transfers intermediate bytes to ComfyUI's input
without a worker disk round-trip. The input copy is retained as
`two_character_pass1_<unique id>.png`; the original SaveImage output also remains.
Job progress stores `image_pass`, `image_passes`, both stage prompts and
`intermediate_image`. Worker logs label `TWO_CHARACTER PASS 1/2` and report
workflow, assigned characters, prompts, seed, steps, CFG, grounding, strengths,
reference boosts and intermediate identifier. Model unload behavior is unchanged.

For QA, restart after current work finishes. In either web or Telegram, activate
one generated scene anchor, select two visually distinct characters for a
two-character image, and keep their order unchanged across these requests:

1. A and B standing beside each other.
2. A and B seated across a table.
3. A and B seated beside each other.
4. B placing a hand on A's shoulder.
5. A and B in a close embrace.

Use their actual names. For each job, open its Pass 1 input from the identifier in
job diagnostics/worker logs (under the configured ComfyUI `input` directory),
then compare it with the delivered final. Score each character's identity,
environment/camera/lighting fidelity, and requested action. Repeat a simple case
in both aspect ratios and both content modes. Confirm only the final is delivered.

Two renders increase latency. The second unmasked inference can alter A or the
scene, and the staging prompt may leave insufficient space or prematurely draw B.
These are visual QA risks, not properties proven by offline tests. No masks,
automatic ordering, resume-from-intermediate logic, or video changes are added.

## Identity routing and wardrobe final state

Character image edits now resolve `workflow_image` through
`resolve_identity_workflow`. Native Identity Edit overrides are accepted; legacy
generic reference overrides are logged and replaced by the current mode/aspect
native preset. Background insertion retains its current native baseline. The
older generic workflows are retained for explicitly separate callers; text-to-image
and clothed look-candidate generation keep their existing graphs.

The shared composer resolves clear wardrobe changes before assembly. Explicit
latest requests override the planned scene; completed removal/replacement in the
scene overrides its superseded outfit clause. Stored design and reference clothing
are not emitted as competing wardrobe rules. An unchanged outfit in the scene may
remain. This handles clear wardrobe edits, not arbitrary story contradictions.

The reproduced failing job 386 combined `wearing fancy lingerie` with `top now
fully removed`. Resolution emits a topless final state and retains identity,
setting and pose. Other examples: a blue dress overridden by `fully nude` loses
the dress; a business suit `changed into a red evening gown` becomes the gown.

Chat follow-up image jobs retain the latest user message for wardrobe precedence,
using the shared web/Telegram completion path. Before rendering, worker progress
records `image_prompt` and `image_workflow`; worker logs print the resolved prompt.
No generated prompts or runtime history are committed to the repository.

For generic text-to-image graphs, adult-mode submission replaces the old enhancer
instruction assuming covered anatomy with an instruction preserving the requested
final consenting-adult wardrobe state. SFW and force-SFW look submissions retain
their previous enhancer instruction. Native identity graphs do not use that legacy
enhancer. Video routing and the sequential insertion design are unchanged.

Manual QA: restart after any running job finishes, then use one adult reference for
`preserve the original outfit`, `wear a red evening gown`, `topless`, `fully nude`,
`same outfit, seated at the bar`, and `same identity in a garden`. Inspect each job's
resolved `image_prompt` and `image_workflow` before evaluating the image. Confirm
old garments disappear only on wardrobe overrides, stable physical identity
instructions remain, and the workflow uses native grounded Identity Edit nodes.
Repeat a safe outfit/pose case in SFW mode. For scene insertion, verify image 1 is
the environment and image 2 the identity; a lone identity still uses image 1.
