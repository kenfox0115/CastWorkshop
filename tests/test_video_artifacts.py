"""Regression checks for subtitle leakage and exact H3 speech guides."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from worker.video_contract import VideoRequest
from worker import comfy_client as cc

ROOT = Path(__file__).resolve().parents[1]

class VideoArtifactTests(unittest.TestCase):
    def test_image_removes_speech_cues_but_preserves_actions_and_signage(self):
        source = 'Alex waves and says "Hello!" in a warm tone. Then Alex points to a sign marked "EXIT".'
        visual = VideoRequest.create(source, 'Alex').visual()
        self.assertIn('Alex waves', visual)
        self.assertIn('Then Alex points to a sign marked "EXIT"', visual)
        for unwanted in ('Hello', 'says', 'speech occurs', 'warm tone'):
            self.assertNotIn(unwanted, visual)
        prompt = VideoRequest.create(source, 'Alex').compile()
        self.assertEqual(prompt.count('<d>[English] Hello!</d>'), 1)
        self.assertIn('No subtitles', prompt)
        self.assertIn('remains silent', prompt)
        self.assertIn('no crowd chatter', prompt)

    def test_postposed_speech_and_payload_line_do_not_leak_into_image(self):
        self.assertNotIn('says Alex', VideoRequest.create('"Hello," says Alex. Alex waves.', 'Alex').visual())
        visual = VideoRequest.create('Alex waves while saying in a warm tone', 'Alex', 'Hello').visual()
        self.assertEqual(visual, 'Alex waves')

    def test_initial_and_continuation_graphs_guide_speech_and_silent_tail(self):
        for aspect in ('vertical', 'widescreen'):
            for kind in ('ref2v', 'continue'):
                with self.subTest(aspect=aspect, kind=kind):
                    graph = json.loads((ROOT / 'worker/workflows/presets' / f'minimax_h3_{kind}_{aspect}.json').read_text())
                    cc._apply_h3_speech_guide(graph, 'exact.flac', 5.0)
                    self.assertEqual(graph['126']['inputs']['conditioning'], ['147', 0])
                    self.assertEqual(graph['147']['inputs']['audio'], ['150', 0])
                    self.assertEqual(graph['148']['inputs']['audio'], 'exact.flac')
                    self.assertEqual(graph['150']['inputs']['audio1'], ['148', 0])
                    self.assertEqual(graph['150']['inputs']['audio2'], ['149', 0])
                    self.assertGreater(graph['149']['inputs']['duration'], 5.0)
                    if kind == 'continue':
                        self.assertEqual(graph['147']['inputs']['image'], ['137', 0])
                    self.assertEqual(graph['136']['inputs']['ref_audios.ref_audio_0'], ['141', 0])

    def test_no_guide_leaves_initial_graph_unchanged(self):
        graph = json.loads((ROOT / 'worker/workflows/presets/minimax_h3_ref2v_vertical.json').read_text())
        original = json.dumps(graph)
        cc._apply_h3_speech_guide(graph, None, 5.0)
        self.assertEqual(json.dumps(graph), original)

    def test_saved_voice_works_without_volatile_comfy_history(self):
        history = Mock(); history.json.return_value = {}
        result = Mock(); result.json.return_value = {'p': {'outputs': {
            cc.MOSS_SAVE_NODE: {'audio': [{'filename': 'exact.flac', 'subfolder': 'audio'}]}}}}
        with patch.object(cc.requests, 'get', side_effect=[history, result]), \
             patch.object(cc, 'submit_prompt', return_value='p') as submit, \
             patch.object(cc, 'promote_output_to_input', return_value='exact.flac'), \
             patch.object(cc, 'free_vram'):
            audio = cc.generate_moss_line_from_saved_voice('saved.flac', 'Hello, honey.')
        graph = submit.call_args.args[0]
        self.assertEqual(audio, 'exact.flac')
        self.assertEqual(graph['6']['inputs']['model_variant'], '1.7B')
        self.assertEqual(graph['12']['inputs']['audio'], 'saved.flac')
        self.assertEqual(graph[cc.MOSS_VOICE_DESIGN_NODE]['inputs']['text'], 'Hello, honey.')
        self.assertEqual(graph[cc.MOSS_VOICE_DESIGN_NODE]['inputs']['opt_narrator'], ['12', 0])
        self.assertEqual(graph['10']['inputs']['value'], [cc.MOSS_VOICE_DESIGN_NODE, 0])
