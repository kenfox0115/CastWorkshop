"""Source-owned video instructions shared by image, H3 and continuation paths.

Keep the authored timeline intact instead of guessing motion from prepositions.
Speech spans are typed separately so quoted signage cannot become dialogue.
This module does not call services or change model/workflow settings.
"""
from dataclasses import dataclass
import re

SPEECH = r"(?:say(?:s|ing)?|said|speak(?:s|ing)?|whisper(?:s|ing|ed)?|shout(?:s|ing|ed)?|repl(?:y|ies|ied)|ask(?:s|ed|ing)?|murmur(?:s|ed|ing)?|tell(?:s|ing)?|respond(?:s|ed|ing)?)"
QUOTES = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|(?<!\w)\x27([^\x27\n]+)\x27(?!\w)')
VISIBLE_TEXT = re.compile(r'\b(?:sign|title|titled|label|caption|called|marked|reading|reads|written|inscription)\b', re.I)


def normalize_quotes(text):
    text = (text or '').translate(str.maketrans({'“':'"','”':'"','„':'"','‟':'"','«':'"','»':'"'}))
    return QUOTES.sub(lambda m: '"' + next(x for x in m.groups() if x is not None) + '"', text)


@dataclass(frozen=True)
class SpeechSpan:
    start: int
    end: int
    line: str


def speech_spans(text):
    spans = []
    for match in QUOTES.finditer(text or ''):
        prefix = (text or '')[:match.start()]
        clause = re.split(r'[.!?;\n]', prefix)[-1]
        suffix = text[match.end():]
        verb = re.search(rf'\b{SPEECH}\b[^.!?;\n]*$', clause, re.I)
        visible = VISIBLE_TEXT.search(clause)
        # A visible-text noun after the last speech verb takes precedence.
        if visible and (not verb or visible.start() > verb.start()):
            continue
        if verb or re.search(r'\b[\w-]+\s*:\s*$', clause) or re.match(rf'\s*,?\s*{SPEECH}\b', suffix, re.I):
            spans.append(SpeechSpan(match.start(), match.end(), next(x for x in match.groups() if x is not None)))
    return tuple(spans)


def dialogue_candidates(text):
    """Quotes needing speech attribution/confirmation, excluding visible text."""
    result = []
    explicit = {s.start for s in speech_spans(text)}
    for match in QUOTES.finditer(text or ''):
        clause = re.split(r'[.!?;\n]', text[:match.start()])[-1]
        if match.start() in explicit or not VISIBLE_TEXT.search(clause):
            result.append(SpeechSpan(match.start(), match.end(), next(x for x in match.groups() if x is not None)))
    return tuple(result)


def non_speech_text(text):
    """Hide all quoted text when examining instructions, including visible labels."""
    return QUOTES.sub('', text or '')


def has_unresolved_speech(text, pattern):
    text = non_speech_text(text)
    # Negation scopes end at punctuation or a new affirmative clause.
    text = re.sub(r'\b(?:no|without|never|not)\b(?:(?!\b(?:but|then)\b|\band\s+\w+\s+(?:says?|speaks?|laughs?)\b)[^.!?;\n])*', '', text, flags=re.I)
    return bool(pattern.search(text))


@dataclass(frozen=True)
class VideoRequest:
    source: str
    actor: str
    speech: tuple
    extra_line: str = ''

    @classmethod
    def create(cls, source, actor, spoken_line=None):
        source = (source or '').strip()
        spans = speech_spans(source)
        line = (spoken_line or '').strip()
        if line and not spans and source.endswith(line):
            # The compatibility extractor also repairs a missing closing quote.
            start = len(source) - len(line) - 1
            if start >= 0 and source[start] in ('"', '“', "'") and re.search(rf'\b{SPEECH}\b', source[:start], re.I):
                spans = (SpeechSpan(start, len(source), line),)
        if any(s.line == line for s in spans):
            line = ''
        if not spans and re.search(r'\b(?:no\s+(?:audible\s+)?(?:speech|dialogue)|without\s+speaking|silently|completely\s+silent)\b', non_speech_text(source), re.I):
            line = ''
        # Never permit control tags inside source text or authored speech.
        if re.search(r'<\s*/?(?:d|audio|picture|subject)\b', source + line, re.I):
            raise ValueError('Video instructions contain reserved media tags')
        return cls(source, actor or 'the character', spans, line)

    def visual(self):
        text = self.source
        for span in reversed(self.speech):
            prefix, suffix = text[:span.start], text[span.end:]
            # Remove attribution too: speech cues can become pseudo-subtitles.
            prefix = re.sub(rf'\b(?:(?:and|then|while|before|after)\s+)?{SPEECH}\b[^.!?;\n]*$', '', prefix, flags=re.I)
            suffix = re.sub(r'^\s*,?\s*(?:in|with)\s+(?:an?\s+)?[^.!?;\n]*?\b(?:tone|voice)\b', '', suffix, flags=re.I)
            suffix = re.sub(rf'^\s*,?\s*{SPEECH}\s+[\w-]+', '', suffix, flags=re.I)
            text = prefix.rstrip() + ' ' + suffix.lstrip()
        if self.extra_line:
            text = re.sub(rf'\b(?:(?:and|then|while)\s+)?{SPEECH}\b[^.!?;\n]*$', '', text, flags=re.I)
        return re.sub(r'\s+', ' ', text).strip()

    def spoken_words(self):
        """The speech guide and H3 must use the same ordered words."""
        return tuple(span.line for span in self.speech) + ((self.extra_line,) if self.extra_line else ())

    def timeline(self, has_audio=True):
        voice = '<Audio 1>' if has_audio else '<S1>'
        def line_block(line):
            return f'using {voice}: <d>[English] {line}</d>'
        text = self.source
        for span in reversed(self.speech):
            text = text[:span.start] + line_block(span.line) + text[span.end:]
        if self.extra_line:
            # Classifier-supplied line without a source location: keep it once.
            if re.search(rf'\b{SPEECH}\b[^.!?]*$', text, re.I):
                text += ' ' + line_block(self.extra_line) + '.'
            else:
                text += f' {self.actor} speaks exactly once {line_block(self.extra_line)}.'
        return text

    def music(self):
        text = non_speech_text(self.source)
        if re.search(r'\b(?:no|without)\s+(?:background\s+|non-diegetic\s+)?music\b', text, re.I):
            return 'N/A'
        clauses = re.split(r'[.;\n]', text)
        requested = [c.strip() for c in clauses if re.search(r'\b(?:music|soundtrack)\b', c, re.I)]
        return '; '.join(requested) if requested else 'N/A'

    def compile(self, has_audio=True, continuation=False):
        start = (
            'Begin from the exact pixels and visible state of <Picture 1> at 0.00 seconds. '
            'Continue forward in time; do not replay, reverse, or reenact a completed action. '
            if continuation else
            'Live-action, cinematic, anchored by <Picture 1>. Begin in the exact pose shown in the image, '
        )
        start += (
            'then perform the new authored timeline below. Preserve identity, face, hair and body proportions. '
            'Preserve the existing setting, clothing state, props, lighting and camera unless the request '
            'explicitly changes that detail. These are starting-state constraints, not a lock on requested motion. '
            'Keep each action with its named actor and object, in the stated order. '
            'Gaze, facing, limb movement, object movement, character travel and camera movement are distinct. '
            'Use one continuous shot with no cut or camera movement unless explicitly requested. '
            'No subtitles, captions, dialogue text, or text overlays. '
        )
        audio = (
            'Speak only the authored dialogue blocks, exactly once in their timeline positions. '
            'Keep delivery outside the spoken words. Lip movement starts with the first word and ends with the last. '
            if self.speech or self.extra_line else
            'No spoken words. '
        )
        audio += 'Only explicitly requested nonverbal sounds are allowed; no invented voices, filler or vocalizations.'
        audio += (' After the final authored line, the speaker closes their mouth and remains silent '
                  'for the rest of the clip; no trailing speech, babble, or background voices.')
        return (
            '[Shot 1] ' + start + '\nAuthored timeline: ' + self.timeline(has_audio) + '\n' + audio
            + '\n\noverall_soundscape: Only ambience supported by the established scene and sounds explicitly '
            'requested in the timeline; non-vocal ambience only, with no crowd chatter or background speech. '
            'Honor explicit silence. Where dialogue is requested, use clear close-recorded '
            'speech with minimal reverberation unless a different acoustic effect is explicitly requested.'
            + '\n\nnon_diegetic_music: ' + self.music()
        )
