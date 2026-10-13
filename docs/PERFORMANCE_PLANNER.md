# Two-character performance planning

The two-character H3 path separates authored intent from restrained performance
inference. Participants, identities, exact dialogue and order, explicit actions,
wardrobe, setting and camera intent remain source-owned. The planner may choose
small breathing, blinking, gaze, posture and silent-listening behaviors where
unspecified. Motionless requests and explicit prohibitions override those defaults.
Listener behavior does not create additional dialogue or lip-sync activity.

Plans contain per-character behavior and a timeline of opening, authored actions,
dialogue, reactions and ending beats. The current workflow supports 5-10 seconds.
Speech/action estimates guide duration; requests that cannot fit are rejected rather
than silently truncating dialogue. Immediate speech and explicit timing take priority
where they fit the supported timeline.

`worker/performance_plan.py` supplies intent extraction, bounded enrichment,
validation and deterministic fallback. The worker makes at most one repair call with
validation errors. Timeouts, malformed output or failed repair use the fallback,
which retains the same timeline and character structure. Planner/repair/fallback
status is recorded separately.

Opening-frame wardrobe edits establish the scene-image state. The video describes
that state; explicitly authored changes occurring during the video remain actions.
The H3 compiler validates dialogue blocks and speaker/audio references, checks for
unresolved tags and stray attribution fragments, and states non-vocal soundtrack
boundaries outside authored dialogue.

These contracts reduce prompt ambiguity. They do not guarantee intelligible speech,
correct motion or identity consistency in generated video. Inspect real audio and
character behavior as part of [release QA](RELEASE_CHECKLIST.md).
