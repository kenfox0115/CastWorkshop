"""Source-owned intent and bounded two-character H3 performance planning.

The model chooses from a small motion vocabulary, not arbitrary action prose.
Authored actions and dialogue remain literal. No GPU or network dependencies.
"""
from copy import deepcopy
import math
import re

VERSION = "performance-timeline-v3"
MIN_DURATION, MAX_DURATION = 5.0, 10.0
MOTION = {
    "breathing": "natural quiet breathing",
    "blinking": "occasional natural blinking",
    "gaze": "a gentle gaze toward the other subject",
    "posture": "small natural posture adjustments",
    "listening": "subtle silent attentive gaze",
    "still": "the requested motionless pose and fixed gaze",
}
NEUTRAL_DELIVERY = {"natural conversational delivery", "neutral measured delivery"}
NUMBER = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half)"
NUMBERS = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11)))
NUMBERS["half"] = 0.5


def seconds(value):
    return float(NUMBERS[value.lower()]) if value.lower() in NUMBERS else float(value)


def canonical(text):
    return re.findall(r"[\w']+", text.casefold())


def finished_wardrobe(visual):
    """Only imperative scene edits become opening state; timed changes stay actions."""
    pattern = re.compile(
        r"\breplace\s+the\s+clothing\s+of\s+(both\s+characters|both\s+subjects)\s+with\s+([^.!?;]+)", re.I)
    visual = pattern.sub(lambda m: "Both characters are wearing " + m.group(2).strip(), visual)
    visual = re.sub(r"\b(?:dress|clothe)\s+(?:both\s+(?:characters|subjects)|them)\s+in\s+([^.!?;]+)",
                    lambda m: "Both characters are wearing " + m.group(1).strip(), visual, flags=re.I)
    return visual


def constraints(source, names):
    from worker.video_contract import non_speech_text
    text = non_speech_text(source)
    result = {}
    for key, name in names.items():
        aliases = [name, name.split()[0]]
        # Titles often precede the given name in stored identities.
        if name.split()[0].lower() in ("captain", "dr", "doctor", "sir", "commander") and len(name.split()) > 1:
            aliases.append(name.split()[1])
        clauses = [c for c in re.split(r"[.!?;\n]", text) if any(
            re.search(rf"\b{re.escape(alias)}\b", c, re.I) for alias in aliases)
            or re.search(r"\b(?:both|they|characters|subjects|everyone)\b", c, re.I)]
        # An unqualified imperative applies to the visible cast.
        clauses += [c for c in re.split(r"[.!?;\n]", text)
                    if re.match(r"\s*(?:remain|stay|keep|do not|don't|no)\b", c, re.I)]
        relevant = " ".join(clauses)
        still = bool(re.search(r"\b(?:motionless|immobile|frozen|perfectly still|completely still)\b", relevant, re.I))
        forbidden = []
        for token, stems in (("blinking", "blink"), ("breathing", "breath"),
                             ("gaze", "look|gaze|eye movement"), ("posture", "move|movement|posture")):
            if re.search(rf"\b(?:no|without|never|do not|don't)\s+(?:\w+\s+){{0,2}}(?:{stems})\w*\b", relevant, re.I):
                forbidden.append(token)
        if re.search(r"\b(?:fixed gaze|fixed eyes|keep looking)\b", relevant, re.I):
            forbidden.append("gaze")
        if re.search(r"\b(?:looks?|looking|gazes?|gazing)\s+(?:at|toward|towards|out|away|down|up)\b", relevant, re.I):
            forbidden.append("gaze")
        result[key] = {"motionless": still, "forbidden": sorted(set(forbidden))}
    return result


def silent_cast(source, names):
    """Explicit silence beats stale separate dialogue payloads."""
    from worker.video_contract import non_speech_text
    text = non_speech_text(source)
    silent = set()
    for clause in re.split(r"[.!?;\n]|\b(?:while|and|but)\b", text, flags=re.I):
        if re.search(r"\b(?:outside|except|other than)\b", clause, re.I):
            continue
        if not re.search(r"\b(?:remain(?:s)? silent|stay(?:s)? silent|without speaking|no (?:speech|dialogue)|do(?:es)? not speak|silent scene)\b", clause, re.I):
            continue
        if re.search(r"\b(?:both|they|characters|subjects)\s+(?:(?:remain|stay|are)\s+)?silent\b|\b(?:silent scene|no dialogue|no speech)\b", clause, re.I):
            silent.update(names)
        else:
            for key, name in names.items():
                parts = name.split()
                aliases = [name, parts[0], parts[1] if len(parts) > 1 and parts[0].lower() == "captain" else parts[0]]
                if any(re.search(rf"\b{re.escape(a)}\s+(?:(?:remains?|stays?|is)\s+)?silent\b|\b{re.escape(a)}\s+(?:does not speak|without speaking)\b", clause, re.I) for a in aliases):
                    silent.add(key)
    return silent


def timing(source, turns):
    from worker.video_contract import non_speech_text
    text = non_speech_text(source)
    first_quote = re.search(r'"', source)
    prefix = source[:first_quote.start()] if first_quote else text
    immediate = bool(re.search(r"\b(?:immediately|right away|at once)\b|\b(?:speech|dialogue)\s+(?:starts?|begins?)\s+at\s+(?:0|zero)\b", prefix, re.I))
    opening = 0.0 if immediate else 0.4
    explicit_opening = immediate
    wait = re.search(rf"\b(?:wait|pause|hold|silent opening)(?:s|ing)?\s+(?:for\s+)?({NUMBER})\s*(?:s\b|seconds?\b)", prefix, re.I)
    at = re.search(rf"\b(?:starts?|begins?)\s+(?:speaking|speech|dialogue)\s+(?:at|after)\s+({NUMBER})\s*(?:s\b|seconds?\b)", prefix, re.I)
    if wait or at:
        opening = seconds((at or wait).group(1))
        explicit_opening = True
    ending = 0.5
    ending_match = re.search(rf"\b(?:end|ending|after the (?:final|last) line)[^.!?]{{0,45}}?({NUMBER})\s*(?:s\b|seconds?\b)", text, re.I)
    if ending_match:
        ending = seconds(ending_match.group(1))
    action_lengths = []
    for turn in turns:
        lengths = {}
        for field in ("action_before", "action_after"):
            action = turn.get(field, "")
            explicit = re.search(rf"\b(?:for|over)\s+({NUMBER})\s*(?:s\b|seconds?\b)", action, re.I)
            verbs = len(re.findall(r"\b(?:walk|lead|open|close|kiss|embrac|remov|turn|point|nod|wink|wave|take|lean|touch|hold|grasp|squeez|pull|push)\w*", action, re.I))
            lengths[field] = seconds(explicit.group(1)) if explicit else (min(5.0, 1.5 + verbs * 1.25 + len(action.split()) * .1) if action else 0.0)
        action_lengths.append(lengths)
    return {"opening": opening, "explicit_opening": explicit_opening,
            "ending": ending, "explicit_ending": bool(ending_match), "actions": action_lengths}


def make_intent(source, names, visual, image_scene, camera, ambience, turns, music="", continuation=False):
    actions = []
    quotes = list(re.finditer(r'"[^"\n]*"', source))
    for clause in re.split(r"(?<=[.!?;])\s+", visual):
        text = clause.strip(" .;")
        verb = re.search(r"\b(?:walks?|steps?|enters?|exits?|turns?|opens?|closes?|nods?|waves?|winks?|removes?|takes?|leads?|puts?|places?|embraces?|kisses?|leans?|points?)\b", text, re.I)
        if not verb or re.search(r"\b(?:not|never|no|don't|doesn't|without)\b", text, re.I):
            continue
        # Continuous setting descriptions stay visible throughout the shot.
        if re.search(r"\b(?:while|are walking|is walking)\b", text, re.I):
            continue
        position = source.casefold().find(text.casefold())
        if position < 0:
            short = text
            for name in names.values():
                parts = name.split()
                alias = parts[1] if parts[0].lower() == "captain" and len(parts) > 1 else parts[0]
                short = short.replace(name, alias)
            position = source.casefold().find(short.casefold())
        slot = sum(q.end() <= position for q in quotes) if position >= 0 else 0
        duration = min(3.0, 1.5 + .75 * len(re.findall(r"\b(?:walk|step|enter|exit|turn|open|close|nod|wave|wink|remov|take|lead|put|place|embrac|kiss|lean|point)\w*", text, re.I)))
        explicit = re.search(rf"\b(?:for|over)\s+({NUMBER})\s*(?:s\b|seconds?\b)", text, re.I)
        actions.append({"text": text, "before_turn": min(slot, len(turns)),
                        "seconds": seconds(explicit.group(1)) if explicit else duration})
    return {"source": source, "cast": names, "image_scene": image_scene, "scene_actions": actions,
            "visual_scene": finished_wardrobe(visual), "camera": camera,
            "ambience": ambience, "music": music, "turns": deepcopy(turns), "continuation": continuation,
            "constraints": constraints(source, names), "timing": timing(source, turns)}


def default_motion(intent, key, listener=False):
    rule = intent["constraints"][key]
    if rule["motionless"]:
        return ["still"]
    choices = ["breathing", "blinking", "gaze", "posture"]
    choices = [c for c in choices if c not in rule["forbidden"]]
    if listener and "gaze" not in rule["forbidden"]:
        choices = [c for c in choices if c != "gaze"] + ["listening"]
    return choices


def speech_duration(line, delivery):
    words = len(re.findall(r"\w+(?:['’]\w+)?", line))
    return round(max(.6, words / 3.0) + (.25 if delivery not in NEUTRAL_DELIVERY else 0), 2)


def build_timeline(plan):
    """Assign literal actions/lines once; reject rather than truncate an overfull shot."""
    intent = plan["intent"]
    time = 0.0
    beats = []

    def beat(kind, duration, turn_index=None, speaker=None, text=""):
        nonlocal time
        if duration <= 0:
            return
        behavior = {key: list(plan["characters"][key]["listener" if kind == "dialogue" and key != speaker else "motion"])
                    for key in ("a", "b")}
        entry = {"kind": kind, "start": round(time, 2), "end": round(time + duration, 2),
                 "behavior": behavior, "audio": "authored_dialogue" if kind == "dialogue" else "non_vocal"}
        if turn_index is not None:
            entry["turn_index"] = turn_index
        if speaker:
            entry["speaker"] = speaker
        if text:
            entry["text"] = text
        beats.append(entry)
        time = round(time + duration, 2)

    beat("opening", plan["opening_seconds"])
    for index, turn in enumerate(plan["turns"]):
        for action in intent["scene_actions"]:
            if action["before_turn"] == index:
                beat("scene_action", action["seconds"], text=action["text"])
        lengths = intent["timing"]["actions"][index]
        beat("action_before", lengths["action_before"], index, turn["speaker"], turn["action_before"])
        beat("dialogue", speech_duration(turn["line"], turn["delivery"]), index, turn["speaker"], turn["line"])
        beat("action_after", lengths["action_after"], index, turn["speaker"], turn["action_after"])
        if index + 1 < len(plan["turns"]):
            beat("reaction", plan["reaction_seconds"], index)
    for action in intent["scene_actions"]:
        if action["before_turn"] == len(plan["turns"]):
            beat("scene_action", action["seconds"], text=action["text"])
    if time + plan["ending_seconds"] > MAX_DURATION + .001:
        raise ValueError("Authored dialogue, actions and timing exceed the supported 10-second shot; split the request")
    # Fill the workflow's minimum duration with non-vocal ending behavior.
    visual_time = min(3.0, .75 * len(re.findall(
        r"\b(?:danc|approach|lean|kiss|touch|hold|grasp|squeez|pull|push|open|close|turn|look|step|wink|nod|wave)\w*\b",
        intent["visual_scene"], re.I)))
    if intent["timing"]["explicit_ending"] and time + plan["ending_seconds"] < MIN_DURATION:
        raise ValueError("Explicit timing is shorter than the workflow's 5-second minimum; extend or split the request")
    ending = max(plan["ending_seconds"] + (0 if intent["timing"]["explicit_ending"] else visual_time), MIN_DURATION - time)
    ending = min(ending, MAX_DURATION - time)
    beat("ending", ending)
    plan["timeline"] = beats
    plan["duration_seconds"] = round(time, 2)
    return plan


def complete_fallback(intent):
    plan = {"version": VERSION, "intent": deepcopy(intent),
            "visual_scene": intent["visual_scene"], "image_scene": intent["image_scene"],
            "camera": intent["camera"], "ambience": intent["ambience"],
            "turns": deepcopy(intent["turns"]), "characters": {},
            "opening_seconds": intent["timing"]["opening"],
            "ending_seconds": intent["timing"]["ending"], "reaction_seconds": .25,
            "planner_status": "fallback", "repair_status": "not_attempted",
            "fallback_reason": "deterministic_default", "validation_errors": []}
    for key, name in intent["cast"].items():
        plan["characters"][key] = {"name": name, "identity_reference": 1 if key == "a" else 2,
            "wardrobe": "established_opening_state", "motion": default_motion(intent, key),
            "listener": default_motion(intent, key, listener=True)}
    # A long but supported request takes priority over optional lead-in/tail.
    try:
        return build_timeline(plan)
    except ValueError:
        if not intent["timing"]["explicit_opening"]:
            plan["opening_seconds"] = 0.0
        if not intent["timing"]["explicit_ending"]:
            plan["ending_seconds"] = 0.0
        plan["reaction_seconds"] = 0.0
        return build_timeline(plan)


def validate(plan, intent):
    """Return actionable errors instead of comparing enrichment to empty hints."""
    errors = []
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    if plan.get("version") != VERSION:
        errors.append("version must be " + VERSION)
    if plan.get("intent") != intent:
        errors.append("immutable intent changed; copy the supplied intent exactly")
    for field in ("visual_scene", "image_scene", "camera", "ambience"):
        if plan.get(field) != intent[field]:
            errors.append(field + " changed immutable scene intent")
    turns = plan.get("turns")
    if not isinstance(turns, list) or len(turns) != len(intent["turns"]):
        errors.append("turns must contain every authored dialogue turn exactly once")
    else:
        for index, (turn, authored) in enumerate(zip(turns, intent["turns"])):
            if not isinstance(turn, dict):
                errors.append(f"turn {index} must be an object")
                continue
            for field in ("speaker", "line", "action_before", "action_after", "speaker_name"):
                if turn.get(field) != authored.get(field):
                    errors.append(f"turn {index} {field} changed authored intent")
            delivery = turn.get("delivery")
            allowed = NEUTRAL_DELIVERY if authored["delivery"] == "natural conversational delivery" else {authored["delivery"]}
            if not isinstance(delivery, str) or delivery not in allowed:
                errors.append(f"turn {index} delivery invents emotion or changes explicit delivery")
    characters = plan.get("characters")
    if not isinstance(characters, dict) or set(characters) != {"a", "b"}:
        errors.append("characters must cover exactly both visible cast members a and b")
    else:
        for key in ("a", "b"):
            char = characters[key]
            if not isinstance(char, dict):
                errors.append(f"character {key} must be an object")
                continue
            if (char.get("name") != intent["cast"][key] or char.get("identity_reference") != (1 if key == "a" else 2)
                    or char.get("wardrobe") != "established_opening_state"):
                errors.append(f"character {key} identity or wardrobe mapping changed")
            for field in ("motion", "listener"):
                value = char.get(field)
                permitted = set(default_motion(intent, key, field == "listener"))
                if (not isinstance(value, list) or any(not isinstance(v, str) or v not in permitted for v in value)
                        or (permitted and not value) or len(set(value)) != len(value)):
                    errors.append(f"character {key} {field} must use restrained allowed behavior {sorted(permitted)}")
                elif "breathing" in permitted and "breathing" not in value or "blinking" in permitted and "blinking" not in value:
                    errors.append(f"character {key} {field} needs breathing and blinking unless explicitly prohibited")
                elif field == "listener" and "listening" in permitted and "listening" not in value:
                    errors.append(f"character {key} listener needs silent attentive behavior")
    for field, explicit, authored in (("opening_seconds", intent["timing"]["explicit_opening"], intent["timing"]["opening"]),
                                      ("ending_seconds", intent["timing"]["explicit_ending"], intent["timing"]["ending"]),
                                      ("reaction_seconds", False, .25)):
        value = plan.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            errors.append(field + " must be a finite nonnegative number")
        elif explicit and value != authored or not explicit and value > .75:
            errors.append(field + " must respect explicit timing or be at most 0.75 seconds")
    if errors:
        return errors
    try:
        expected = build_timeline(deepcopy(plan))
    except ValueError as exc:
        return [str(exc)]
    if plan.get("timeline") != expected["timeline"]:
        errors.append("timeline must contain continuous opening, authored actions/dialogue, reactions and ending with both characters and audio boundaries; use the supplied scheduler")
    if plan.get("duration_seconds") != expected["duration_seconds"]:
        errors.append("duration must equal the complete timeline within 5 to 10 seconds")
    return errors


def model_schema(template):
    """Performance-only response; no arbitrary visible action or soundtrack prose."""
    return {"type": "object", "required": ["characters", "deliveries", "opening_seconds", "ending_seconds", "reaction_seconds"],
        "properties": {
            "characters": {"type": "object", "required": ["a", "b"], "properties": {
                key: {"type": "object", "required": ["motion", "listener"], "properties": {
                    field: {"type": "array", "items": {"type": "string", "enum": list(MOTION)}}
                    for field in ("motion", "listener")}, "additionalProperties": False}
                for key in ("a", "b")}, "additionalProperties": False},
            "deliveries": {"type": "array", "items": {"type": "string"}},
            **{field: {"type": "number"} for field in ("opening_seconds", "ending_seconds", "reaction_seconds")}},
        "additionalProperties": False}


def proposal(template):
    return {"characters": {key: {field: deepcopy(template["characters"][key][field]) for field in ("motion", "listener")}
                           for key in ("a", "b")},
            "deliveries": [turn["delivery"] for turn in template["turns"]],
            **{field: template[field] for field in ("opening_seconds", "ending_seconds", "reaction_seconds")}}


def merge_proposal(template, value):
    """Compose intent with model performance, rejecting extra free-text fields."""
    if not isinstance(value, dict):
        return None, ["performance response must be an object"]
    allowed = set(proposal(template))
    if set(value) != allowed:
        # Keep the historical diagnostic for legacy visual-invention responses.
        reason = "visual_rewrite_not_source_grounded" if "visual_scene" in value else "performance response has missing or unknown fields"
        return None, [reason]
    candidate = deepcopy(template)
    for field in ("opening_seconds", "ending_seconds", "reaction_seconds"):
        candidate[field] = value[field]
    if not isinstance(value["characters"], dict) or set(value["characters"]) != {"a", "b"}:
        return None, ["characters must cover a and b"]
    for key in ("a", "b"):
        if not isinstance(value["characters"][key], dict) or set(value["characters"][key]) != {"motion", "listener"}:
            return None, [f"character {key} must supply motion and listener arrays only"]
        candidate["characters"][key].update(deepcopy(value["characters"][key]))
    deliveries = value["deliveries"]
    if not isinstance(deliveries, list) or len(deliveries) != len(candidate["turns"]):
        return None, ["deliveries must match the exact number of authored turns"]
    for turn, delivery in zip(candidate["turns"], deliveries):
        turn["delivery"] = delivery
    # Validate bounded performance before the scheduler touches malformed input.
    partial = deepcopy(candidate)
    partial["timeline"] = template["timeline"]
    errors = [e for e in validate(partial, template["intent"]) if not e.startswith(("timeline must", "duration must"))]
    if errors:
        return None, errors
    try:
        build_timeline(candidate)
    except ValueError as exc:
        return None, [str(exc)]
    return candidate, validate(candidate, template["intent"])


def plan_with_model(template, call, parse, model=None, keep_alive=None):
    import json
    desired = proposal(template)
    prompt = (
        "Plan restrained performance for every visible character throughout this shot. "
        "Code owns the supplied immutable intent and schedules every exact action and dialogue line. "
        "Return the performance object only, matching the schema. Choose only allowed behavior tokens. "
        "Breathing and blinking are required unless prohibited; gaze, posture, and silent attentive listening are optional. "
        "Listener lips remain still. Motionless or negated behavior overrides defaults. "
        "Preserve explicit delivery; otherwise use neutral delivery. Add no emotion, physical contact, props, people, speech or vocalizations. "
        "Default non-vocal opening/ending and reaction pauses must be <=0.75s; explicit timing wins.\n"
        "Immutable intent: " + json.dumps(template["intent"], ensure_ascii=False) + "\n"
        "Valid complete performance example: " + json.dumps(desired, ensure_ascii=False))
    fallback = deepcopy(template)
    errors = []
    for attempt in range(2):
        try:
            raw = call(prompt, json_mode=model_schema(template), model=model, keep_alive=keep_alive)
        except Exception as exc:
            fallback["planner_status"] = "fallback"
            fallback["repair_status"] = "failed" if attempt else "not_attempted"
            fallback["fallback_reason"] = f"{'repair' if attempt else 'planner'}_error:{type(exc).__name__}"
            fallback["validation_errors"] = errors
            return fallback
        try:
            value = parse(raw)
            candidate, current_errors = merge_proposal(template, value)
        except (TypeError, ValueError, KeyError) as exc:
            value = None
            candidate, current_errors = None, [f"invalid JSON or performance: {type(exc).__name__}"]
        if candidate is not None and not current_errors:
            candidate.update(planner_status="repaired" if attempt else "model",
                             repair_status="succeeded" if attempt else "not_attempted",
                             fallback_reason="", validation_errors=errors)
            return candidate
        errors = current_errors
        # Preserve individually valid proposed values in the repair example.
        retained = deepcopy(desired)
        if isinstance(value, dict):
            for field in desired:
                trial = deepcopy(retained)
                trial[field] = value.get(field, trial[field])
                _, trial_errors = merge_proposal(template, trial)
                if not trial_errors:
                    retained = trial
            if isinstance(value.get("characters"), dict):
                for key in ("a", "b"):
                    proposed_char = value["characters"].get(key)
                    if not isinstance(proposed_char, dict):
                        continue
                    for field in ("motion", "listener"):
                        trial = deepcopy(retained)
                        trial["characters"][key][field] = proposed_char.get(field, trial["characters"][key][field])
                        _, trial_errors = merge_proposal(template, trial)
                        if not trial_errors:
                            retained = trial
            if isinstance(value.get("deliveries"), list) and len(value["deliveries"]) == len(desired["deliveries"]):
                for index, delivery in enumerate(value["deliveries"]):
                    trial = deepcopy(retained)
                    trial["deliveries"][index] = delivery
                    _, trial_errors = merge_proposal(template, trial)
                    if not trial_errors:
                        retained = trial
        prompt += ("\nRepair this response once. Validation errors: " + json.dumps(errors)
                   + "\nPrevious response: " + json.dumps(value, ensure_ascii=False)
                   + "\nKeep valid parts; corrected valid example: " + json.dumps(retained, ensure_ascii=False))
    fallback.update(planner_status="fallback", repair_status="failed",
                    fallback_reason="visual_rewrite_not_source_grounded" if "visual_rewrite_not_source_grounded" in errors else "repair_validation_failed",
                    validation_errors=errors)
    return fallback


def delivery_phrase(value):
    if value == "natural conversational delivery":
        return "speaks naturally"
    if value == "neutral measured delivery":
        return "speaks naturally at a measured pace"
    if value.endswith("ly"):
        return "speaks " + value
    if value.startswith(("a ", "an ")):
        return "speaks with " + value
    return "speaks in " + ("an " if value[:1].lower() in "aeiou" else "a ") + value + " tone"


def compile_prompt(plan):
    errors = validate(plan, plan["intent"])
    if errors:
        raise ValueError("Invalid performance plan: " + "; ".join(errors))
    names = plan["intent"]["cast"]
    speakers = list(dict.fromkeys(t["speaker"] for t in plan["turns"]))
    definitions = [f"<Subject {i}> is {names[key]}, whose visual identity comes from <Picture {i}>."
                   for i, key in enumerate(("a", "b"), 1)]
    definitions.append("<Picture 3> is the target composition and opening-scene reference.")
    for index, key in enumerate(speakers, 1):
        definitions.append(f"<Audio {index}> is the voice-timbre reference for <Subject {1 if key == 'a' else 2}> (S{index}); its recorded words are excluded from the target soundtrack.")
    beats = []
    for beat in plan["timeline"]:
        span = f"[{beat['start']:g}-{beat['end']:g}s]"
        kind = beat["kind"]
        if kind == "dialogue":
            turn = plan["turns"][beat["turn_index"]]
            key = turn["speaker"]
            audio = speakers.index(key) + 1
            text = (f"<Subject {1 if key == 'a' else 2}> (S{audio}) {delivery_phrase(turn['delivery'])}, "
                    f"using the voice timbre referenced from <Audio {audio}>: <d>[English] {turn['line']}</d>. "
                    "Lip movement begins with the first word and ends with the last.")
            other = "b" if key == "a" else "a"
            listener = ", ".join(MOTION[token] for token in beat["behavior"][other]) or "the authored resting pose"
            text += f" <Subject {1 if other == 'a' else 2}> maintains {listener}; listener lips remain still."
        elif kind.startswith("action_") or kind == "scene_action":
            text = beat["text"] + ". Both subjects remain silent during this action."
            if kind == "action_before":
                text += " After it is complete, the authored line follows."
        else:
            text = ("After the final line, " if kind == "ending" and speakers else "") + kind.capitalize() + " beat: non-vocal ambience; both subjects remain naturally engaged without further speech or speech articulation."
        beats.append(span + " " + text)
    motion = " ".join(f"<Subject {1 if key == 'a' else 2}>: " + (", ".join(MOTION[token] for token in plan["characters"][key]["motion"]) or "maintain the authored pose") + "." for key in ("a", "b"))
    scene_state = plan["visual_scene"]
    for action in plan["intent"]["scene_actions"]:
        scene_state = scene_state.replace(action["text"], "", 1)
    scene_state = re.sub(r"\s+", " ", scene_state).strip(" .;") or "Both subjects occupy the established setting"
    music = " Diegetic music remains audible during [Shot 1]: " + plan["intent"]["music"] + "." if plan["intent"]["music"] else ""
    return ("subject_definitions:\n" + "\n".join(definitions)
            + "\n\nsummary:\n" + VERSION + ": one continuous cinematic shot; "
            + ("only authored dialogue is voiced." if speakers else "silent scene, non-vocal ambience only.")
            + "\n\nretention_analysis:\nPreserve facial identity and build throughout. Wardrobe begins in the established opening state and follows only authored changes. "
              "Picture 3 establishes opening positions, setting, lighting and props; motion follows the authored timeline and per-character constraints."
            + "\n\ndetailed_description:\n[Shot 1] Begin from <Picture 3>. " + scene_state.rstrip(".") + ". "
            + plan["camera"].rstrip(".") + ". " + motion + music + " " + " ".join(beats)
            + "\n\noverall_soundscape: " + plan["ambience"].rstrip(".")
            + "; non-vocal ambience outside authored dialogue. There is no human speech or vocalization outside the authored dialogue."
            + "\n\nnon_diegetic_music: N/A")


def validate_compiled(prompt, plan, speakers=None, duration=None):
    """Validate literal compiled text and its reference order before submission."""
    if prompt.strip() != compile_prompt(plan).strip():
        raise ValueError("Compiled performance prompt differs from validated timeline or has unresolved template tags")
    expected_speakers = list(dict.fromkeys(t["speaker"] for t in plan["turns"]))
    if speakers is not None and list(speakers) != expected_speakers:
        raise ValueError("Compiled speaker/audio-reference mapping differs from authored order")
    if duration is not None and abs(duration - plan["duration_seconds"]) > .001:
        raise ValueError("Workflow duration differs from validated timeline")
    blocks = re.findall(r"<d>\[English\] (.*?)</d>", prompt, re.S)
    if blocks != [t["line"] for t in plan["turns"]]:
        raise ValueError("Compiled prompt duplicates or changes authored dialogue")
    allowed_tags = r"</?d>|<(?:Subject|Picture|Audio) [123]>"
    if re.search(r"<[^>]*>|\{\{[^}]*\}\}", re.sub(allowed_tags, "", prompt)):
        raise ValueError("Compiled prompt contains unresolved template tags")
    scene = plan["visual_scene"]
    for name in plan["intent"]["cast"].values():
        for alias in (name, *name.split()):
            if re.search(rf"(?:^|[.!?])\s*{re.escape(alias)}\s*(?:[.!?]|$)", scene):
                raise ValueError("Compiled prompt contains dangling speech attribution")
    if re.search(r"\breplace the clothing\b", scene, re.I):
        raise ValueError("Opening wardrobe edit remains a video action")
    return True
