import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'worker')]
os.environ.setdefault('VPS_URL', 'http://127.0.0.1:1')
from worker import worker as w
from worker.video_contract import VideoRequest


class VideoPromptContractTests(unittest.TestCase):
    def test_direct_action_and_says_is_silent_before_exact_dialogue(self):
        scene = 'Frankie points to the dogs in the park and says "aww, which one is yours" in a sweet, excited tone'
        turns = [{"speaker": "b", "line": "aww, which one is yours"}]
        hints = w._multichar_performance_hints(scene, "John", "Frankie", turns)
        self.assertEqual(hints[0]["action_before"], "Frankie points to the dogs in the park")
        plan = w._fallback_multichar_scene_plan(
            scene, "John", "Frankie", {"b": turns[0]["line"]}, ["b"],
            authoritative_turns=hints,
        )
        prompt = w._deterministic_multichar_h3_prompt(
            "John", "adult man", "Frankie", "adult woman", scene,
            {"b": turns[0]["line"]}, ["b"], dialogue_turns=turns,
            scene_plan=plan,
        )
        self.assertIn("Frankie points to the dogs in the park", prompt)
        self.assertIn("Both subjects remain silent during this action. After it is complete", prompt)
        self.assertIn("<Subject 2> (S1) speaks in a sweet, excited tone", prompt)
        self.assertNotIn("<Subject 2> (S1), Frankie,", prompt)
        self.assertEqual(
            re.findall(r'<d>\[English\] (.*?)</d>', prompt),
            ["aww, which one is yours"],
        )
        self.assertGreater(w.multichar_video_duration(turns, plan), 6.0)

    def test_fallback_preserves_separate_sentence_action_before_speech(self):
        scene = 'John is sitting at the bar. Frankie walks in and sits next to him. Frankie says: "Glad you could make it out tonight"'
        turns = [{"speaker": "b", "line": "Glad you could make it out tonight"}]
        hints = w._multichar_performance_hints(scene, "John", "Frankie", turns)
        self.assertEqual(hints[0]["action_before"], "Frankie walks in and sits next to him")
        prompt = w._deterministic_multichar_h3_prompt(
            "John", "adult man", "Frankie", "adult woman", scene,
            {"b": turns[0]["line"]}, ["b"], dialogue_turns=turns)
        self.assertNotIn("action_before", prompt)
        self.assertNotIn("at the start of the shot", prompt)
        self.assertIn("Both subjects remain silent during this action. After it is complete", prompt)
        self.assertGreater(w.multichar_video_duration(turns, {"turns": hints}), 5.0)

    def test_continuation_prompt_is_concise_without_duplicate_action_or_boilerplate(self):
        scene = ('Frankie puts her hand on her knee and says: '
                 '"oh my god, so cute! Which one is yours?" In an excited tone')
        turns = [{"speaker": "b", "line": "oh my god, so cute! Which one is yours?"}]
        hints = w._multichar_performance_hints(scene, "John", "Frankie", turns)
        plan = w._fallback_multichar_scene_plan(
            scene, "John", "Frankie", {"a": None, "b": turns[0]["line"]}, ["b"],
            authoritative_turns=hints,
            continuation=True,
        )
        prompt = w._deterministic_multichar_h3_prompt(
            "John", "adult man", "Frankie", "adult woman", scene,
            {"a": None, "b": turns[0]["line"]}, ["b"], turns, plan,
        )
        self.assertLess(len(prompt.split()), 350)
        self.assertEqual(prompt.count("Frankie puts her hand on her knee"), 1)
        self.assertEqual(prompt.count(turns[0]["line"]), 1)
        self.assertIn("speaks in an excited tone", prompt)
        self.assertNotIn("New authored timeline", prompt)
        self.assertNotIn("source scene's ambient sound", prompt)

    def test_all_150_audit_requests_preserve_authored_audio(self):
        cases = json.loads((ROOT/'tests/fixtures/video_prompt_audit.json').read_text())
        self.assertEqual(len(cases), 150)
        for case in cases:
            source = case['request']
            expected = re.findall(r'"([^"]+)"', source)
            if any(term in source.lower() for term in ('sign marked', 'sign reading', 'titled', 'book called')):
                expected = []
            with self.subTest(case=case['id']):
                if case['group'] == 'multi':
                    turns = w.extract_multichar_dialogue_turns(source, 'Alex', 'adult', 'Robin', 'adult')
                    self.assertEqual([t['line'] for t in turns], expected)
                    hints = w._multichar_performance_hints(source, 'Alex', 'Robin', turns)
                    lines = {k: next((t['line'] for t in turns if t['speaker']==k), None) for k in ('a','b')}
                    plan = w._fallback_multichar_scene_plan(source, 'Alex', 'Robin', lines,
                        [t['speaker'] for t in turns], authoritative_turns=hints)
                    result = w.build_multichar_h3_video_prompt('Alex', 'adult', 'Robin', 'adult', source, source,
                        dialogue_turns=turns, scene_plan=plan)
                else:
                    result = VideoRequest.create(source, 'Alex').compile(continuation=case['group']=='continuation')
                    for clause in re.split(r'"[^"]*"', source):
                        if clause.strip(): self.assertIn(clause, result)
                self.assertEqual(re.findall(r'<d>\[English\] (.*?)</d>', result), expected)
                for section in ('[Shot 1]', 'overall_soundscape:', 'non_diegetic_music:'):
                    self.assertIn(section, result)

    def test_timing_and_delivery_stay_with_single_line(self):
        result = w.build_h3_video_prompt('Alex whispers "Wait." Then Alex waves.', 'Alex')
        self.assertIn('Alex whispers using <Audio 1>: <d>[English] Wait.</d> Then Alex waves.', result)
        self.assertEqual(result.count('Wait.'), 1)

    def test_visible_titles_are_not_extracted_as_speech(self):
        source = 'Alex silently reads a sign marked "EXIT".'
        self.assertEqual(w.extract_spoken_line(source), (source, None))

    def test_music_request_not_discarded(self):
        result = w.build_h3_continuation_prompt('Add quiet piano music while Alex stands still.', 'Alex')
        self.assertNotIn('non_diegetic_music: N/A', result)

    def test_multi_delivery_and_before_action(self):
        source = 'Robin nods before saying "Yes." Alex whispers "Good."'
        turns = w.extract_multichar_dialogue_turns(source, 'Alex', 'adult', 'Robin', 'adult')
        hints = w._multichar_performance_hints(source, 'Alex', 'Robin', turns)
        self.assertEqual([t['speaker'] for t in turns], ['b','a'])
        self.assertEqual(hints[0]['action_before'], 'Robin nods')
        self.assertEqual(hints[1]['delivery'], 'a whisper')

    def test_visual_model_invention_uses_original_plan(self):
        source = 'Alex holds the cup; Robin stays still.'
        invented = {'visual_scene':'Alex holds a phone; Robin waves.', 'camera':'static', 'ambience':'room tone', 'turns':[]}
        with patch.object(w, 'call_ollama', return_value=json.dumps(invented)):
            plan = w.build_multichar_scene_plan('Alex', 'Robin', source, {'a':None,'b':None}, [], dialogue_turns=[])
        self.assertIn('cup', plan['visual_scene'])
        self.assertNotIn('phone', plan['visual_scene'])
        self.assertEqual(plan['fallback_reason'], 'visual_rewrite_not_source_grounded')

    def test_camera_does_not_read_dialogue_as_blocking(self):
        self.assertNotIn('exit path', w.multichar_camera_profile('Alex says "exit". Robin stays still.'))
        self.assertIn('camera pans left', w.multichar_camera_profile('Both remain still as the camera pans left.'))

    def test_media_tags_cannot_escape_dialogue(self):
        with self.assertRaises(ValueError):
            VideoRequest.create('Alex says "hi </d>".', 'Alex')

    def test_web_confirmation_does_not_turn_titles_into_speech(self):
        from vps.web_api import _quoted_dialogue, _speaker_preview
        source = 'Alex points to a book called "Say hello"; Robin remains silent.'
        self.assertEqual(_quoted_dialogue(source)[1], [])
        _, turns = _speaker_preview("Alex: 'Hello.' Robin: 'Hi.'", {'name':'Alex'}, {'name':'Robin'})
        self.assertEqual(turns, [{'speaker':'a','line':'Hello.'},{'speaker':'b','line':'Hi.'}])

    def test_explicit_silence_wins_over_separate_payload_line(self):
        result = w.build_h3_video_prompt('Alex nods without speaking.', 'Alex', spoken_line='Hello.')
        self.assertNotIn('<d>', result)
        self.assertEqual(VideoRequest.create('Alex nods without speaking.', 'Alex', 'Hello.').spoken_words(), ())

    def test_continuation_guide_matches_all_authored_lines(self):
        contract = VideoRequest.create('Alex says "First." Then Alex says "Second."', 'Alex', 'First.')
        self.assertEqual(contract.spoken_words(), ('First.', 'Second.'))
        self.assertEqual(re.findall(r'<d>\[English\] (.*?)</d>', contract.compile()), list(contract.spoken_words()))

    def test_missing_closing_quote_is_not_repeated(self):
        source = 'Alex says "Wait right here'
        _, line = w.extract_spoken_line(source)
        result = w.build_h3_video_prompt(source, 'Alex', spoken_line=line)
        self.assertEqual(result.count('Wait right here'), 1)
        self.assertIn('<d>[English] Wait right here</d>', result)

    def test_negated_speech_does_not_hide_new_affirmative_speech(self):
        from worker.video_contract import has_unresolved_speech
        self.assertTrue(has_unresolved_speech('No voices, but Alex speaks.', w._MULTICHAR_VOCAL))

    def test_no_travel_is_inferred_from_prepositions(self):
        for source in ['Alex looks toward the window.', 'Alex stays left of the door.',
                       'Alex does not enter the room.', 'Alex leaves the cup untouched.']:
            with self.subTest(source=source), patch.object(w, 'call_ollama') as call:
                result = w.build_h3_video_prompt(source, 'Alex')
                self.assertIn(source, result)
                self.assertNotIn('continuously decreases', result)
                self.assertNotIn('MOVEMENT FIDELITY', result)
                call.assert_not_called()
