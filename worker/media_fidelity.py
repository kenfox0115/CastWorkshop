"""Deterministic checks that NSFW media plans preserve explicit adult requests."""
import re


_GROUPS = {
    "bare_breasts": (
        r"\b(?:topless|bare[- ]chested)\b|"
        r"\b(?:show|flash|expos|uncover|bar|see|view)\w*\b.{0,40}\b(?:tits?|boobs?|breasts?|nipples?|chest)\b|"
        r"\b(?:tits?|boobs?|breasts?|nipples?|chest)\b.{0,30}\b(?:out|bare|exposed|uncovered|visible)\b",
        r"\b(?:topless|bare|exposed|uncovered|visible|nude)\b.{0,45}\b(?:breasts?|chest|nipples?)\b|"
        r"\b(?:breasts?|chest|nipples?)\b.{0,45}\b(?:bare|exposed|uncovered|visible|nude)\b",
        "her breasts and nipples are fully bare and clearly visible, with no top, bra, fabric, or obstruction covering them",
    ),
    "nudity": (
        r"\b(?:nude|naked|fully unclothed)\b",
        r"\b(?:nude|naked|fully unclothed)\b",
        "the adult reference character is fully nude",
    ),
    "under_skirt_reveal": (
        r"\bupskirt\b|"
        r"\b(?:what(?:'s| is)|show|reveal|see|view)\b.{0,45}\b(?:under|beneath|up)\b.{0,25}\b(?:skirt|dress|gown)\b|"
        r"\b(?:lift|raise|pull)\w*\b.{0,30}\b(?:skirt|dress|gown)\b.{0,30}\b(?:show|reveal|see|view)\b",
        r"(?=[\s\S]*\b(?:skirt|dress|gown)\b.{0,45}\b(?:lifted|raised|pulled up|held up|open)\b|"
        r"[\s\S]*\b(?:lifted|raised|pulled up|held up|open)\b.{0,45}\b(?:skirt|dress|gown)\b)"
        r"(?=[\s\S]*\b(?:vulva|pussy|labia|pubic hair|bare lower body|panties|underwear)\b)",
        "she faces the camera in a stable standing pose with her skirt lifted high; the area beneath "
        "the skirt is plainly visible, with her bare vulva and natural pubic hair unobstructed",
    ),
    "female_genitals": (
        r"\b(?:show|expose|spread|open)\w*\b.{0,40}\b(?:pussy|vulva|labia|clitoris|clit)\b|"
        r"\b(?:pussy|vulva|labia|clitoris|clit)\b.{0,30}\b(?:visible|exposed|open|spread)\b",
        r"\b(?:pussy|vulva|labia|clitoris|clit)\b",
        "she sits upright facing the camera in a stable full-body pose, both legs fully visible and bent at the knees with her feet visible and set apart; her vulva is clearly visible in its natural anatomical position at the pelvis between her thighs; both arms and hands remain visible and away from her torso",
    ),
    "labia_spread": (
        r"\b(?:lift|pull|part|open|spread)\w*\b.{0,35}\b(?:pussy lips|labia)\b|"
        r"\b(?:pussy lips|labia)\b.{0,35}\b(?:lifted|pulled|parted|open|spread)\b",
        r"\b(?:lift|pull|part|open|spread)\w*\b.{0,35}\b(?:pussy lips|labia)\b|"
        r"\b(?:pussy lips|labia)\b.{0,35}\b(?:lifted|pulled|parted|open|spread)\b",
        "the adult reference character visibly parts her labia as requested",
    ),
    "male_genitals": (
        r"\b(?:show|expose)\w*\b.{0,40}\b(?:cock|dick|penis|testicles|balls)\b|"
        r"\b(?:cock|dick|penis|testicles|balls)\b.{0,30}\b(?:visible|exposed|out|bare)\b",
        r"\b(?:cock|dick|penis|testicles|balls)\b",
        "he stands facing the camera in a stable full-body pose with both legs, feet, arms, and hands fully visible; his penis and testicles are clearly visible in their natural anatomical position at the pelvis between his thighs",
    ),
    "masturbation": (
        r"\b(?:masturbat(?:e|es|ing|ion)|fingering|touch(?:ing)? (?:herself|himself))\b|"
        r"\b(?:finger|touch|rub|stroke)\w*\b.{0,40}\b(?:pussy|vulva|labia|clitoris|clit|cock|dick|penis)\b|"
        r"\b(?:pussy|vulva|labia|clitoris|clit|cock|dick|penis)\b.{0,40}\b(?:finger|touch|rub|stroke)\w*\b",
        r"(?=[\s\S]*\b(?:finger|touch|rub|stroke)\w*\b.{0,45}\b(?:pussy|vulva|labia|clitoris|clit|cock|dick|penis)\b|"
        r"[\s\S]*\b(?:pussy|vulva|labia|clitoris|clit|cock|dick|penis)\b.{0,45}\b(?:finger|touch|rub|stroke)\w*\b)"
        r"(?=[\s\S]*\b(?:nude|naked|bare|exposed|uncovered|visible|panties (?:are )?removed|panties (?:are )?pulled aside|dress (?:is )?lifted|gown (?:is )?lifted|skirt (?:is )?lifted)\b)",
        "she sits upright facing the camera in a stable full-body pose with both legs and feet visible; her lower body is nude and one hand visibly rubs her vulva while her other hand remains visible away from her pelvis",
    ),
    "breast_squeeze": (
        r"\bsqueez\w*\b.{0,40}\b(?:breast|tit|boob)s?\b|"
        r"\b(?:breast|tit|boob)s?\b.{0,40}\bsqueez\w*\b",
        r"\bsqueez\w*\b.{0,40}\b(?:breast|tit|boob)s?\b|"
        r"\b(?:breast|tit|boob)s?\b.{0,40}\bsqueez\w*\b",
        "one hand visibly squeezes one bare breast from the side while the other hand remains relaxed and fully visible",
    ),
    "breast_grab": (
        r"\b(?:grab|grasp)\w*\b.{0,40}\b(?:breast|tit|boob)s?\b|"
        r"\b(?:breast|tit|boob)s?\b.{0,40}\b(?:grab|grasp)\w*\b",
        r"\b(?:grab|grasp)\w*\b.{0,40}\b(?:breast|tit|boob)s?\b|"
        r"\b(?:breast|tit|boob)s?\b.{0,40}\b(?:grab|grasp)\w*\b",
        "one hand visibly grasps one bare breast from the side while the other hand remains relaxed and fully visible",
    ),
    "breast_cup": (
        r"\b(?:cup|hold)\w*\b.{0,40}\b(?:breast|tit|boob)s?\b|"
        r"\b(?:breast|tit|boob)s?\b.{0,40}\b(?:cup|hold)\w*\b",
        r"\b(?:cup|hold)\w*\b.{0,40}\b(?:breast|tit|boob)s?\b|"
        r"\b(?:breast|tit|boob)s?\b.{0,40}\b(?:cup|hold)\w*\b",
        "one hand visibly cups one bare breast from below while the other hand remains relaxed and fully visible",
    ),
    "butt_squeeze": (
        r"\bsqueez\w*\b.{0,40}\b(?:ass|butt|buttock|ass cheek)s?\b|"
        r"\b(?:ass|butt|buttock|ass cheek)s?\b.{0,40}\bsqueez\w*\b",
        r"\bsqueez\w*\b.{0,40}\b(?:ass|butt|buttock|ass cheek)s?\b|"
        r"\b(?:ass|butt|buttock|ass cheek)s?\b.{0,40}\bsqueez\w*\b",
        "in a stable rear three-quarter standing pose, one hand visibly squeezes the near buttock while the other hand remains relaxed and fully visible",
    ),
    "butt_grab": (
        r"\b(?:grab|grasp)\w*\b.{0,40}\b(?:ass|butt|buttock|ass cheek)s?\b|"
        r"\b(?:ass|butt|buttock|ass cheek)s?\b.{0,40}\b(?:grab|grasp)\w*\b",
        r"\b(?:grab|grasp)\w*\b.{0,40}\b(?:ass|butt|buttock|ass cheek)s?\b|"
        r"\b(?:ass|butt|buttock|ass cheek)s?\b.{0,40}\b(?:grab|grasp)\w*\b",
        "in a stable rear three-quarter standing pose, one hand visibly grasps the near buttock while the other hand remains relaxed and fully visible",
    ),
    "genital_grab": (
        r"\b(?:grab|grasp|hold|squeeze)\w*\b.{0,40}\b(?:cock|dick|penis|testicles|balls)\b|"
        r"\b(?:cock|dick|penis|testicles|balls)\b.{0,40}\b(?:grab|grasp|hold|squeeze)\w*\b",
        r"\b(?:grab|grasp|hold|squeeze)\w*\b.{0,40}\b(?:cock|dick|penis|testicles|balls)\b|"
        r"\b(?:cock|dick|penis|testicles|balls)\b.{0,40}\b(?:grab|grasp|hold|squeeze)\w*\b",
        "in a stable front-facing full-body pose, one hand visibly holds his bare penis while the other hand remains relaxed and fully visible",
    ),
    "oral_sex": (
        r"\b(?:blowjob|oral sex|cunnilingus)\b|"
        r"\bsuck(?:s|ing)?\b.{0,60}\b(?:cock|dick|penis)\b|"
        r"\b(?:cock|dick|penis)\b.{0,40}\b(?:mouth|lips)\b|"
        r"\b(?:mouth|lips)\b.{0,40}\b(?:cock|dick|penis)\b",
        r"\b(?:blowjob|oral sex|cunnilingus)\b|"
        r"\bsuck(?:s|ing)?\b.{0,60}\b(?:cock|dick|penis)\b|"
        r"\b(?:cock|dick|penis)\b.{0,40}\b(?:mouth|lips)\b|"
        r"\b(?:mouth|lips)\b.{0,40}\b(?:cock|dick|penis)\b",
        "the adult reference character kneels in a stable pose and visibly performs the requested oral action on the off-screen adult partner's penis; the partner's face, torso, and limbs remain outside the frame",
    ),
}

_REMOVAL_REQUEST = re.compile(
    r"\b(?:pull|lower|slide)\b.{0,35}\b(?:down|off)\b|"
    r"\b(?:take|peel|strip)\b.{0,35}\boff\b|"
    r"\b(?:remove|strip|undress|expose|uncover)(?:s|ed|ing)?\b",
    re.I,
)
_REMOVAL_SCENE = re.compile(
    r"\b(?:pull(?:s|ed|ing)?|lower(?:s|ed|ing)?|slide(?:s|d|ing)?|take(?:s|n|ing)?|"
    r"peel(?:s|ed|ing)?|remov(?:e|es|ed|ing)|strip(?:s|ped|ping)?|undress(?:es|ed|ing)?|"
    r"expos(?:e|es|ed|ing)|uncover(?:s|ed|ing)?)\b",
    re.I,
)
_COMPLETED_REMOVAL_SCENE = re.compile(
    r"\b(?:garment|top|shirt|blouse|bra|dress|gown|skirt|lingerie|underwear|panties|"
    r"clothes|clothing|outfit)\b.{0,45}\b(?:is |are |has been )?"
    r"(?:off|removed|lowered|pulled down|slid down|taken off|stripped off|open)\b|"
    r"\b(?:removed|lowered|pulled down|slid down|taken off|stripped off)\b.{0,45}"
    r"\b(?:garment|top|shirt|blouse|bra|dress|gown|skirt|lingerie|underwear|panties|"
    r"clothes|clothing|outfit)\b|"
    r"\b(?:fully|clearly|visibly)\s+(?:exposed|uncovered|undressed|topless|nude|naked)\b|"
    r"\b(?:bare breasts?|bare chest|nude|naked|topless|fully unclothed)\b",
    re.I,
)


def nsfw_requirements(request, require_completed_removal=False):
    request = request or ""
    requirements = [name for name, (source, _target, _fallback) in _GROUPS.items()
                    if re.search(source, request, re.I)]
    if _REMOVAL_REQUEST.search(request):
        requirements.append(
            "clothing_removal_complete" if require_completed_removal
            else "clothing_removal"
        )
    return requirements


def missing_requirements(request, scene, require_completed_removal=False):
    scene = scene or ""
    missing = []
    for name in nsfw_requirements(request, require_completed_removal):
        if name == "clothing_removal":
            if not _REMOVAL_SCENE.search(scene):
                missing.append(name)
        elif name == "clothing_removal_complete":
            if not _COMPLETED_REMOVAL_SCENE.search(scene):
                missing.append(name)
        elif not re.search(_GROUPS[name][1], scene, re.I):
            missing.append(name)
    return missing


def fallback_scene(missing):
    phrases = []
    for name in missing:
        if name in ("clothing_removal", "clothing_removal_complete"):
            phrases.append("the requested garment is visibly pulled down or removed as requested")
        else:
            phrases.append(_GROUPS[name][2])
    return "The adult reference character is alone in the established setting; " + "; ".join(phrases) + "."
