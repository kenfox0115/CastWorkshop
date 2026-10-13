"""Small, deterministic image instructions; reference roles come from the graph."""
import re
from dataclasses import dataclass


def assemble_final_prompt(*sections):
    """Join nonempty sections once, preserving authored instruction order."""
    return " ".join(dict.fromkeys(s.strip() for s in sections if s and s.strip()))


def requested_action(text):
    """Unwrap our known historical headers, including already queued jobs."""
    text = (text or "").strip()
    for marker in ("Only change the following:", "Requested edit:"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
    return re.sub(r"(?:reference\s+image|picture)\s+(\d+)", r"image \1", text, flags=re.I)


def build_scene_instruction(index):
    return (f"Use image {index} for the environment, layout, camera angle, lighting, "
            "furnishings and props, except changes explicitly requested below.")


def build_identity_instruction(index, name="the character"):
    return (f"Use image {index} for {name}'s facial identity and structure, hair, skin tone, ethnicity "
            "and build, except explicitly requested changes. Clothing, pose and background follow the request, not the identity image.")


_GARMENT = r"(?:evening gowns?|business suits?|dress(?:es)?|gowns?|suits?|lingerie|bikinis?|shirts?|tops?|blouses?|bras?|outfits?|clothes|jackets?|shorts|trousers|pants|jeans|swimsuits?|skirts?|uniforms?|sweaters?)"
_WORN = re.compile(rf"\b(?:wearing|wears|dressed in)\s+(?:[\w-]+\s+){{0,5}}{_GARMENT}\b", re.I)
_TOP_REMOVED = re.compile(r"\b(?:(?:her|his|their|the)\s+)?(?:top|shirt|blouse|bra)\s+(?:(?:is|now|has been)\s+)?(?:(?:fully|completely)\s+)?removed\b", re.I)


def requested_wardrobe_state(text):
    """Recognize explicit final wardrobe states without inventing outfit details."""
    if re.search(r"\b(?:not|never|don't|do not)\b.{0,35}\b(?:nude|naked|topless|remove|take off|expos\w*|reveal\w*|wear\w*|change|put on)\b", text, re.I):
        return None
    if re.search(r"\b(?:fully nude|fully naked|fully unclothed|full nudity|nude|naked)\b", text, re.I):
        return 'fully nude'
    if (re.search(r"\btopless\b|\b(?:bare|exposed|uncovered)\s+breasts\b", text, re.I) or _TOP_REMOVED.search(text)
            or re.search(r"\b(?:remove|take off)\s+(?:(?:your|that|the|her|his|their)\s+)?(?:top|shirt|blouse|bra)\b", text, re.I)
            or re.search(r"\b(?:taking|take|takes|took)\s+(?:(?:that|the|her|his|their)\s+)?top\s+off\b", text, re.I)):
        return 'topless'
    replacement = re.search(rf"\b(?:change(?:d|s)? into|put on)\s+((?:[\w-]+\s+){{0,5}}{_GARMENT})\b", text, re.I)
    if not replacement:
        replacement = re.search(rf"\bwear(?:ing)?\s+((?:[\w-]+\s+){{0,5}}{_GARMENT})\b", text, re.I)
    if not replacement:
        fragment = re.sub(r'^both\s+(?:are\s+)?', '', text.strip(), flags=re.I)
        replacement = re.fullmatch(rf"((?:[\w-]+\s+){{0,5}}{_GARMENT})[.!]?", fragment, re.I)
    if replacement:
        outfit = replacement.group(1)
        outfit = re.sub(r'\bdresses\b', 'dress', outfit, flags=re.I)
        outfit = re.sub(r'\b(gown|suit|bikini|shirt|top|blouse|bra|outfit|jacket|swimsuit|skirt|uniform|sweater)s\b', r'\1', outfit, flags=re.I)
        if not re.match(r'^(?:a|an|the|her|his|their)\b', outfit, re.I) and not re.search(r'\b(?:lingerie|clothes|shorts|trousers|pants|jeans)\b', outfit, re.I):
            outfit = ('an ' if outfit[:1].lower() in 'aeiou' else 'a ') + outfit
        return 'wearing ' + outfit
    return None


def resolve_wardrobe_state(scene, latest_request=None, *, subject="The character", plural=False):
    """Resolve explicit wardrobe overrides without importing stored/source outfits.

    This deliberately handles clear wardrobe edits, not arbitrary story parsing.
    The latest request wins; otherwise completed states in the planned scene win.
    Other actions and physical attributes remain in the scene.
    """
    scene = re.sub(r'\bFinal wardrobe:\s*', '', scene, flags=re.I)
    final = requested_wardrobe_state(latest_request or '')
    # A planned outfit alone is not an override. Completed removal/replacement is.
    if final is None and (re.search(r"\b(?:topless|nude|naked|unclothed|changed into)\b|\b(?:bare|exposed|uncovered)\s+breasts\b", scene, re.I) or _TOP_REMOVED.search(scene)):
        final = requested_wardrobe_state(scene)
    if final is None:
        return scene
    had_outfit = bool(_WORN.search(scene))
    scene = _WORN.sub(lambda m: ('is ' if m.group(0).lower().startswith('wears ') else '') + final, scene)
    scene = _TOP_REMOVED.sub('' if had_outfit else final, scene)
    scene = re.sub(rf"\b(?:remove|take off)\s+(?:(?:the|her|his|their)\s+)?(?:[\w-]+\s+){{0,3}}{_GARMENT}\b", '', scene, flags=re.I)
    scene = re.sub(rf"\bchanged into\s+(?:[\w-]+\s+){{0,5}}{_GARMENT}\b", '', scene, flags=re.I)
    scene = re.sub(r"\b(?:fully\s+)?(?:nude|naked|unclothed|topless)\b", lambda m: final, scene, flags=re.I)
    scene = re.sub(r"\b(?:are|is)\s+(?:exposing|revealing|showing)\s+(?:(?:their|her|his)\s+)?(?:bare|exposed|uncovered)\s+breasts\b", lambda m: 'are topless' if m.group(0).lower().startswith('are ') else 'is topless', scene, flags=re.I)
    scene = re.sub(r"\b(?:instead|so|then)\s+(?:(?:she|he|they)\s+)?(?:is|are|be)\s+", '', scene, flags=re.I)
    scene = re.sub(r"\b(?:She|He|They)\s+(?:(?:is|are)\s*)?(?=[.,;]|$)", '', scene, flags=re.I)
    scene = re.sub(r"\s+([.,;])", r"\1", scene)
    scene = re.sub(r"([.,;])(?:\s*[.,;])+", r"\1 ", scene).strip(' ,.;')
    scene = re.sub(r"\s+", ' ', scene)
    trailing = re.search(rf"[.;]\s*{re.escape(final)}\.?$", scene, re.I)
    if trailing and final.casefold() in scene[:trailing.start()].casefold():
        scene = scene[:trailing.start()].rstrip(' .;')
    if final.casefold() not in scene.casefold():
        scene = assemble_final_prompt(scene.rstrip('.') + '.' if scene else '', f"{subject} {'are' if plural else 'is'} {final}.")
    return scene


def normalize_scene_description(scene, names, latest_request=None):
    """Describe completed wardrobe edits and common fragments without new details.

    Resolve each named subject separately so a change to one person cannot strip
    the other person's outfit. Collective requests apply to both subjects.
    """
    request = requested_action(latest_request) if latest_request else None
    subject = ' and '.join(names) if len(names) > 1 else (names[0] if names[0] != 'the character' else 'The character')
    if re.fullmatch(rf'(?:[\w-]+\s+){{0,5}}{_GARMENT}[.!]?', scene.strip(), re.I):
        scene = requested_wardrobe_state(scene) or scene
    if len(names) == 1:
        scene = resolve_wardrobe_state(scene, request, subject=subject)
    collective = bool(request and re.search(r'\b(?:both|they|their)\b', request, re.I))
    names_pattern = '|'.join(re.escape(n) for n in names)
    # Plural "A and B wear" is a shared clause; singular "B wears"
    # marks a separately authored wardrobe clause.
    separator = r'(?<=[.;])\s+|\s+and\s+(?=(?:' + names_pattern + r')\s+(?:is|wears|changes?|change into|has|stands|sits)\b)'
    clauses = re.split(separator, scene.strip(), flags=re.I) if len(names) > 1 else [scene]
    has_named_clauses = any(re.search(rf'\b(?:{names_pattern})\b', c, re.I) for c in clauses)
    resolved = []
    for clause in clauses:
        clause_names = [n for n in names if re.search(rf'\b{re.escape(n)}\b', clause, re.I)]
        applicable = request
        if collective and has_named_clauses and not clause_names:
            applicable = None
        if len(names) > 1 and request and not collective:
            request_names = [n for n in names if re.search(rf'\b{re.escape(n)}\b', request, re.I)]
            # A request naming just one person applies only to that person's clause.
            if request_names and (not clause_names or set(clause_names) != set(request_names)):
                relevant = [p for p in re.split(separator, request, flags=re.I)
                            if any(re.search(rf'\b{re.escape(n)}\b', p, re.I) for n in clause_names)]
                applicable = ' '.join(relevant) or None
        # Completed states for different people in one clause must stay distinct.
        if len(names) > 1 and not applicable:
            parts = re.split(r'\s*;\s*', clause, flags=re.I)
            clause = '; '.join(resolve_wardrobe_state(p, subject=' and '.join(clause_names) or subject,
                              plural=len(clause_names or names) > 1) for p in parts)
        else:
            clause = resolve_wardrobe_state(clause, applicable,
                subject=' and '.join(clause_names) or subject, plural=len(clause_names or names) > 1)
        resolved.append(clause)
    scene = ' '.join(c.rstrip(' .;') + '.' for c in resolved if c.strip()) if len(resolved) > 1 else ' '.join(resolved).strip()
    plural = len(names) > 1
    # Explicit collective fragments become a grammatical named scene.
    scene = re.sub(r'^both\s+(?:are\s+)?', subject + ' are ', scene, flags=re.I)
    scene = re.sub(r'^they\s+', subject + ' ', scene, flags=re.I)
    scene = re.sub(r'^wear\s+', subject + (' are wearing ' if plural else ' is wearing '), scene, flags=re.I)
    if re.match(r'^(?:standing|sitting|lying|kneeling|wearing|topless|fully nude)\b', scene, re.I):
        scene = scene[:1].lower() + scene[1:]
        if plural:
            scene = re.sub(r'^sitting\s+(?=(?:at|in|on)\b)', 'sitting together ', scene, flags=re.I)
        scene = subject + (' are ' if plural else ' is ') + scene
    elif re.match(r'^(?:at|in|on|beside|near|by)\b', scene, re.I):
        scene = subject + (' are ' if plural else ' is ') + scene
    elif re.match(r'^(?:stand|sit|lie|kneel)\b', scene, re.I):
        scene = subject + ' ' + scene[:1].lower() + scene[1:] if plural else subject + ' ' + re.sub(r'^(stand|sit|lie|kneel)\b', lambda m: {'stand':'stands','sit':'sits','lie':'lies','kneel':'kneels'}[m.group(1).lower()], scene, flags=re.I)
    for name in names:
        scene = re.sub(rf'\b{re.escape(name)}\b', lambda m: name, scene, flags=re.I)
    scene = re.sub(r'\blos angeles\b', 'Los Angeles', scene, flags=re.I)
    scene = re.sub(r'\bcafe\b', 'café', scene, flags=re.I)
    return scene[:1].upper() + scene[1:].rstrip(' .;') + '.' if scene else ''



def compose_chat_media_scene(scene, latest_request="", reply="", mode=None):
    """Resolve authored chat plans before applying the strict render contract.

    This single-character route owns its reference roles. The current user request
    wins over the reply, and both win over stale wardrobe in a generated plan.
    Direct/manual image prompts still use build_image_prompt's strict validation.
    """
    scene = requested_action(scene)
    if mode == "sfw":
        from worker.sfw import sfw_context
        latest_request, reply = sfw_context(latest_request), sfw_context(reply)
        if requested_wardrobe_state(latest_request) in ("fully nude", "topless"):
            latest_request = ""
        if requested_wardrobe_state(reply) in ("fully nude", "topless"):
            reply = ""
    # Reference wiring is application-owned, never an LLM-authored scene fact.
    scene = re.sub(
        r"\b(?:reference\s+)?image\s+\d+\s+(?:is|defines)\s+(?:the\s+)?"
        r"(?:scene|environment|person|character|identity)(?:\s+reference)?\b",
        "The reference character" , scene, flags=re.I,
    )
    # Honor a clear negative reveal request even when the planner invented one.
    if re.search(r"\b(?:not|never|don't|do not)\b.{0,35}\b(?:nude|naked|topless)\b", latest_request or "", re.I):
        scene = re.sub(r"\b(?:fully\s+)?(?:nude|naked|unclothed|topless)\b", "fully clothed", scene, flags=re.I)
    wardrobe_source = latest_request
    if requested_wardrobe_state(latest_request or "") is None:
        # A negative wardrobe request must not be overridden by the drafted reply.
        negative = re.search(r"\b(?:not|never|don't|do not)\b.{0,35}\b(?:nude|naked|topless|remove|wear|change|put on)\b", latest_request or "", re.I)
        if not negative and requested_wardrobe_state(reply or ""):
            wardrobe_source = reply
    source_state = requested_wardrobe_state(wardrobe_source or "")
    scene_state = requested_wardrobe_state(scene)
    conflict = bool(scene_state == "fully nude" and _WORN.search(scene))
    if conflict or (source_state and source_state != scene_state):
        scene = normalize_scene_description(scene, ["the character"], wardrobe_source)
    # Validate the actual assembled prompt; do not suppress or disable its checks.
    build_image_prompt(scene)
    return scene


def _known_gender(metadata):
    """Use explicit metadata only; character names do not imply gender."""
    metadata = metadata or {}
    if isinstance(metadata, str):
        metadata = {'concept': metadata}
    text = metadata.get('gender') or metadata.get('concept') or ''
    match = re.search(r'\b(woman|women|female|man|men|male)\b', text, re.I)
    if match:
        return 'woman' if match.group(1).lower() in ('woman', 'women', 'female') else 'man'


def build_participant_instruction(names, adult_mode=False, character_metadata=None):
    metadata = character_metadata or {}
    genders = [_known_gender(metadata.get(name)) for name in names]
    if len(names) != 2:
        return 'The character is an adult.' if adult_mode else ''
    if all(g == 'woman' for g in genders):
        kind = 'women'
    elif all(g == 'man' for g in genders):
        kind = 'men'
    else:
        kind = 'characters'
    count = f"Show exactly two {'adult ' if adult_mode else ''}{kind}."
    if set(genders) == {'woman', 'man'}:
        count = assemble_final_prompt(count, f"{names[0]} is a {genders[0]}; {names[1]} is a {genders[1]}.")
    return assemble_final_prompt(count, 'Keep their identities distinct. Do not blend or duplicate the characters.')


def build_wardrobe_instructions(scene, names, *, replacement_requested=False):
    """Expand final wardrobe states with scoped, concrete source-clothing rules."""
    groups = {}
    for clause in re.split(r'[.;]\s*', scene):
        final = requested_wardrobe_state(clause)
        if not final or (final not in ('topless', 'fully nude') and not replacement_requested):
            continue
        subjects = [n for n in names if re.search(rf'\b{re.escape(n)}\b', clause, re.I)] or names
        groups.setdefault(final, [])
        groups[final].extend(n for n in subjects if n not in groups[final])
    instructions = []
    for final, subjects in groups.items():
        subject = ' and '.join(subjects)
        plural = len(subjects) > 1
        if final == 'topless':
            instructions.append(f"{subject} {'have bare upper bodies' if plural else 'has a bare upper body'}, "
                'with no bra, shirt, top, jacket, vest, or other upper-body garment. '
                f"Do not preserve conflicting upper-body clothing from {subject}'s reference images.")
        elif final == 'fully nude':
            instructions.append(f"{subject} {'wear' if plural else 'wears'} no clothing or lingerie. "
                f"Do not preserve any conflicting source garments from {subject}'s reference images.")
        elif 'lingerie' in final.lower():
            instructions.append(f"For {subject}, the requested lingerie is the complete final outfit. "
                'Do not preserve jackets, outerwear, or conflicting source clothing from the reference images.')
        else:
            instructions.append(f"{subject}'s requested outfit completely replaces conflicting source wardrobe.")
    return assemble_final_prompt(*instructions)


def build_image_prompt(action, identities=((1, "the character"),), scene_index=None, *, participant_count=None, latest_request=None, adult_mode=False, normalized_scene=False, character_metadata=None):
    """Assemble roles without importing clothing/background from a concept.

    A lone identity uses image 1. Scene insertion uses environment 1, identity 2.
    Multi-character graphs may have two identities and an optional environment.
    """
    indexes = [i for i, _ in identities]
    if len(set(indexes)) != len(indexes) or scene_index in indexes:
        raise ValueError("Scene and identity images must have distinct reference roles")
    authored_replacement = any(marker in str(action) for marker in (
        'requested outfit completely replaces conflicting source wardrobe.',
        'requested lingerie is the complete final outfit.'))
    action = requested_action(action)
    # Reject an ambiguous simultaneous state, rather than guessing which authored
    # clause wins. Different people and explicit transitions are valid requests.
    if (not latest_request and (participant_count or len(identities)) == 1
            and re.search(r"\b(?:fully nude|fully naked|fully unclothed)\b", action, re.I)
            and re.search(r"\bwearing\s+(?:(?:a|an|the|her|his|their)\s+)?(?:\w+\s+){0,2}(?:dress|shirt|suit|lingerie|bikini|outfit|clothes)\b", action, re.I)
            and not re.search(r"\b(?:remove\w*|undress\w*|change\w*|instead|no longer|take\w* off)\b", action, re.I)):
        raise ValueError("Conflicting clothing states: specify the final outfit or an explicit change")
    wardrobe_imperative = bool(re.search(r'\b(?:change(?:d|s)? into|put on)\b|^wear\b', action, re.I)
                              or re.fullmatch(rf'(?:[\w-]+\s+){{0,5}}{_GARMENT}[.!]?', action, re.I))
    if not normalized_scene:
        action = normalize_scene_description(action, [name for _, name in identities], latest_request)
    replacement_requested = bool(authored_replacement or wardrobe_imperative or requested_wardrobe_state(latest_request or ''))
    wardrobe = '' if normalized_scene else build_wardrobe_instructions(action,
        [name for _, name in identities], replacement_requested=replacement_requested)
    # Explicit role instructions in authored text must agree with the actual graph.
    for index in indexes:
        if re.search(rf"\bimage {index}\s+(?:is|defines) (?:the )?(?:scene|environment)\b", action, re.I):
            raise ValueError(f"image {index} is an identity reference, not the environment")
    if scene_index and re.search(rf"\bimage {scene_index}\s+(?:is|defines) (?:the )?(?:person|character|identity)\b", action, re.I):
        raise ValueError(f"image {scene_index} is the environment reference")
    return assemble_final_prompt(
        build_scene_instruction(scene_index) if scene_index else "",
        *(build_identity_instruction(i, name) for i, name in identities),
        build_participant_instruction([name for _, name in identities], adult_mode, character_metadata),
        wardrobe,
        f"Requested edit: {action}" if action else "",
    )


def _project_character_scene(scene, character, other):
    """Keep shared scene facts and this person's clauses, excluding the other person."""
    clauses = re.split(r'[.;]\s*|\s+and\s+(?=' + '(?:' + re.escape(character) + '|' + re.escape(other) + ')' + r'\s+(?:is|wears|has|sits|stands)\b)', scene)
    result = []
    for clause in clauses:
        has_character = bool(re.search(rf'\b{re.escape(character)}\b', clause, re.I))
        has_other = bool(re.search(rf'\b{re.escape(other)}\b', clause, re.I))
        if has_other and not has_character:
            continue
        clause = re.sub(rf'\b(?:{re.escape(character)} and {re.escape(other)}|{re.escape(other)} and {re.escape(character)})\b', character, clause, flags=re.I)
        # Remaining other-person relationships become generic future staging.
        clause = re.sub(rf'\b{re.escape(other)}\b', 'a second person', clause, flags=re.I)
        clause = re.sub(rf'\b{re.escape(character)} are\b', character + ' is', clause, flags=re.I)
        clause = re.sub(rf'\b{re.escape(character)} (sit|stand|wear|have)\b',
                        lambda m: character + ' ' + {'sit':'sits','stand':'stands','wear':'wears','have':'has'}[m.group(1).lower()], clause, flags=re.I)
        clause = re.sub(r'\b(?:together|beside each other)\b', '', clause, flags=re.I)
        clause = re.sub(r'\bfacing one another with amused expressions, smiling naturally as they talk\b',
                        'with an amused expression, smiling naturally while talking', clause, flags=re.I)
        clause = re.sub(r'^both\s+(?:are\s+)?', character + ' is ', clause, flags=re.I)
        clause = re.sub(r'^they\s+(?:are\s+)?', character + ' is ', clause, flags=re.I)
        clause = re.sub(r'\bboth\b', '', clause, flags=re.I)
        clause = re.sub(r'\s+', ' ', clause).strip(' ,.;')
        if clause:
            result.append(clause + '.')
    return ' '.join(result)


@dataclass(frozen=True)
class _CharacterIntent:
    name: str
    pose_action: str
    wardrobe: str


@dataclass(frozen=True)
class _SequentialIntent:
    first: _CharacterIntent
    second: _CharacterIntent


def _character_intent(name, scoped_scene, replacement_requested):
    wardrobe = build_wardrobe_instructions(scoped_scene, [name], replacement_requested=replacement_requested)
    return _CharacterIntent(name, scoped_scene, wardrobe)


def _build_sequential_identity_instruction(index, name):
    return (f"Use image {index} only to preserve {name}'s facial identity and structure, identity-relevant hair color, style and length, "
            "skin tone, ethnicity, body type and build, stable age appearance, and distinctive permanent physical features "
            "such as tattoos, scars, birthmarks, permanent piercings, distinctive moles, or other stable body markings when present. "
            "Identity comes from the reference. Transient visual state comes from the request. "
            "Clothing, jewelry, chains, hats, eyewear, temporary accessories, pose, facial expression, background, camera angle, "
            "framing, lighting, props, and temporary makeup or styling follow the requested final image, not the identity reference; "
            "preserve makeup or styling only when explicitly part of the character design.")


def _parse_sequential_intent(action, first_character, second_character, *, latest_request=None, adult_mode=False, character_metadata=None):
    """Normalize once, then retain only each character's scoped final intent."""
    raw_action = requested_action(action)
    raw_action = re.sub(r'\bhaving a funny conversation\b',
        'facing one another with amused expressions, smiling naturally as they talk', raw_action, flags=re.I)
    # Ensure interaction-only fragments also have a grammatical subject.
    if re.match(r'^facing\b', raw_action, re.I):
        raw_action = f'{first_character} and {second_character} are ' + raw_action[:1].lower() + raw_action[1:]
    names = [first_character, second_character]
    final_scene = normalize_scene_description(raw_action, names, latest_request)
    replacement_requested = bool(requested_wardrobe_state(latest_request or '')
        or re.search(r'\b(?:change(?:d|s)? into|put on)\b|^wear\b', raw_action, re.I)
        or re.fullmatch(rf'(?:[\w-]+\s+){{0,5}}{_GARMENT}[.!]?', raw_action, re.I))
    first_scene = _project_character_scene(final_scene, first_character, second_character)
    first_scene = re.sub(r'\b(?:beside|with|across from|facing) a second person\b', '', first_scene, flags=re.I)
    first_scene = re.sub(r'\s+([.,])', r'\1', first_scene)
    second_scene = _project_character_scene(final_scene, second_character, first_character)
    second_scene = second_scene.replace('a second person', first_character)
    if re.search(r'\bhaving a funny conversation\b', requested_action(action), re.I):
        second_scene = second_scene.replace('with an amused expression, smiling naturally while talking',
            f'facing {first_character} with an amused expression, smiling naturally while talking')
    return _SequentialIntent(
        _character_intent(first_character, first_scene, replacement_requested),
        _character_intent(second_character, second_scene, replacement_requested),
    )


def build_sequential_image_prompts(action, first_character, second_character, *, latest_request=None, adult_mode=False, character_metadata=None):
    """Compose only structured per-pass intent; never append the global request."""
    intent = _parse_sequential_intent(action, first_character, second_character, latest_request=latest_request)
    names = [first_character, second_character]
    metadata = character_metadata or {}
    gender = _known_gender(metadata.get(first_character)) or 'character'
    # Named principals and explicitly authored background crew are separate
    # counts. Keep the default exclusion unless the scene actually includes crew.
    crew_source = latest_request if latest_request and re.search(r'\bcrew\b', latest_request, re.I) else action
    crew_source = crew_source or ''
    background_crew = bool(re.search(
        r'(?:^|\b(?:with|include|show)\s+)(?:the\s+)?background crew\b|'
        r'\b(?:background crew|rest of the crew)\s+(?:work\w*|stand\w*|sit\w*|walk\w*|are|is)\b',
        crew_source, re.I)) and not re.search(
        r"\b(?:no|without|exclude|remove|do not include|do not show|don't include|don't show)\b[^.;]{0,40}\bcrew\b",
        crew_source, re.I)
    first_count = f"Show exactly one {'adult ' if adult_mode else ''}{gender}: {first_character}. Do not generate any additional person."
    if background_crew:
        first_count = (f"Show {first_character} as the only named principal character. "
                       "Include the explicitly requested background crew, with no additional principal characters.")
        if adult_mode:
            first_count += ' All depicted people are adults.'
    first = assemble_final_prompt(
        build_scene_instruction(1), _build_sequential_identity_instruction(2, first_character),
        first_count,
        intent.first.wardrobe,
        f'Leave appropriate open space beside {first_character} for a second person to be added in a later pass.',
        intent.first.pose_action,
    )
    # The final scene's structural constraint is only owned by pass two.
    participants = build_participant_instruction(names, adult_mode, metadata)
    participants = participants.replace('Show exactly two ', 'The final image contains exactly two ', 1)
    participants = participants.replace('. Keep their identities distinct.',
        f': {first_character} and {second_character}. Keep their identities distinct.', 1)
    if background_crew:
        participants = participants.replace('The final image contains exactly two ',
            'The final image contains exactly two named principal ', 1)
        participants += ' Include the explicitly requested background crew.'
        if adult_mode:
            participants += ' All depicted people are adults.'
    second = assemble_final_prompt(
        f'Image 1 is the established scene containing {first_character}. Preserve the environment, composition, camera position, lighting, furnishings, props, and style.',
        f"{first_character} is already correct. Preserve {first_character} exactly as established in image 1, including identity, face, hair, permanent physical features, body, position, pose, expression, and wardrobe state. Do not restyle, redress, replace, reinterpret, or transfer attributes onto {first_character}.",
        f'Only make minimal spatial adjustments if absolutely necessary to place {second_character} naturally without changing {first_character}\'s established appearance.',
        f"Image 2 provides {second_character}'s identity only.",
        _build_sequential_identity_instruction(2, second_character),
        f'Do not copy, transfer, reuse, or apply clothing, accessories, jewelry, chains, pose, background, styling, or other transient visual attributes from image 2 to {first_character}, the environment, or any other part of the final image.',
        'Clothing and accessories visible in image 2 are reference-only and must not appear anywhere in the final image unless explicitly requested.',
        f'Add {second_character} naturally into the established scene beside {first_character}.',
        intent.second.wardrobe,
        participants,
        intent.second.pose_action,
    )
    return first, second


# Trait words are intentional personality/archetype signals, not appearance stereotypes.
_EXPRESSIONS = (
    (r"confident|glamorous|self-assured", "a self-assured gaze and faint knowing smile", "poised posture"),
    (r"shy|sweet|gentle|timid", "soft attentive eyes and a gentle tentative smile", "relaxed, slightly reserved posture"),
    (r"dominant|assertive|sharp|commanding", "steady intense eye contact and a composed mouth", "calm assured posture"),
    (r"playful|flirty|flirtatious|mischievous", "lively eyes and a restrained teasing smile", "easy engaged posture"),
    (r"melancholy|introspective|thoughtful", "a thoughtful gaze and subdued relaxed mouth", "quiet reflective posture"),
)
_EXPLICIT_EXPRESSION = re.compile(
    r"\b(?:smil\w*|frown\w*|grin\w*|laugh\w*|scowl\w*|cry\w*|expression|gaze|eye contact)\b", re.I)


def build_expression_instruction(concept, personality=""):
    """Explicit appearance direction wins; personality supplies restrained cues."""
    if _EXPLICIT_EXPRESSION.search(concept):
        return ""
    source = f"{personality or ''} {concept}"
    matches = [(re.search(rf"\b(?:{words})\b", source, re.I), face, body)
               for words, face, body in _EXPRESSIONS]
    matches = sorted((m.start(), face, body) for m, face, body in matches if m)
    if matches:
        # One facial mood avoids incompatible smiles/gazes; a second trait may
        # soften the posture without piling multiple expressions onto one face.
        face = matches[0][1]
        body = matches[min(1, len(matches) - 1)][2]
    else:
        face, body = "attentive eyes and a relaxed natural mouth", "easy engaged posture"
    return f"Subtle human expression: {face}, with {body}."
