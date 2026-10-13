"""Video opening frames share the image-job sequential composition path."""
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from test_multichar_dialogue import video_worker as worker


class VideoSceneInsertionTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.job = {'id': 687, 'payload': {
            'char_a': {'name': 'Ava', 'concept': 'adult woman', 'base_image': 'a.png', 'voice_sample': 'a.wav'},
            'char_b': {'name': 'Ben', 'concept': 'adult man', 'base_image': 'b.png', 'voice_sample': 'b.wav'},
            'scene_text': 'Ava and Ben stand on the bridge.',
            'scene_reference_image': 'scene.png', 'dialogue_turns': [],
        }}
        self.plan = worker._fallback_multichar_scene_plan(
            self.job['payload']['scene_text'], 'Ava', 'Ben', {'a': None, 'b': None}, [], authoritative_turns=[])
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def mock(obj, name, **kwargs):
            return self.stack.enter_context(patch.object(obj, name, **kwargs))
        mock(worker, 'build_multichar_scene_plan', return_value=self.plan)
        mock(worker, 'evict_current_model')
        mock(worker, 'get_content_mode', return_value='sfw')
        mock(worker, 'get_aspect_mode', return_value='vertical')
        mock(worker, 'video_start_delay_seconds', return_value=0)
        self.cc = worker.comfy_client
        self.legacy = mock(self.cc, 'generate_two_char_edit_image', return_value={'filename': 'legacy.png'})
        self.promote = mock(self.cc, 'promote_output_to_input', return_value='video-scene.png')
        self.video = mock(self.cc, 'generate_multichar_voice_reference_video', return_value=b'video')

    def run_job(self):
        worker.process_multichar_video(self.job, self.client, 'model')

    def test_attached_scene_runs_both_native_passes_and_promotes_only_final(self):
        graphs = []
        with (patch.object(self.cc, '_content_mode', return_value='sfw'),
              patch.object(self.cc, '_aspect_mode', return_value='vertical'),
              patch.object(self.cc, 'submit_prompt', side_effect=lambda graph: graphs.append(graph) or str(len(graphs))),
              patch.object(self.cc, 'wait_for_images', side_effect=[{'filename': 'first.png'}, {'filename': 'final.png', 'subfolder': 'nested'}]),
              patch.object(self.cc, 'prepare_identity_reference', side_effect=lambda name: 'isolated-' + name),
              patch.object(self.cc, 'fetch_image_bytes', return_value=b'first'),
              patch.object(self.cc, 'upload_image_bytes_to_input', return_value='intermediate.png'),
              patch.object(self.cc, 'free_vram')):
            self.run_job()
        self.assertEqual(len(graphs), 2)
        self.assertEqual(graphs[0]['72']['inputs']['image'], 'scene.png')
        self.assertEqual(graphs[0]['90']['inputs']['image'], 'isolated-a.png')
        self.assertEqual(graphs[1]['72']['inputs']['image'], 'intermediate.png')
        self.assertEqual(graphs[1]['90']['inputs']['image'], 'isolated-b.png')
        self.legacy.assert_not_called()
        self.promote.assert_called_once_with('final.png', subfolder='nested')
        self.assertEqual(self.video.call_args.args[2], 'video-scene.png')
        updates = [c.kwargs for c in self.client.update_progress.call_args_list if 'image_pass' in c.kwargs]
        self.assertEqual([u['image_pass'] for u in updates], [1, 2])
        self.client.post.assert_called_once()

    def test_only_opening_intent_enters_image_passes(self):
        self.job['payload']['latest_request'] = 'Ben says "Hello" and then waves.'
        with patch.object(self.cc, 'generate_two_character_scene_image', return_value={'filename': 'final.png'}) as sequential:
            self.run_job()
        self.assertEqual(sequential.call_args.args[5], self.plan['image_scene'])
        self.assertEqual(sequential.call_args.kwargs['latest_request'], self.plan['image_scene'])
        self.assertEqual(sequential.call_args.kwargs['character_metadata']['Ava'], self.job['payload']['char_a'])

    def test_first_pass_failure_stops_before_video_and_completion(self):
        with (patch.object(self.cc, 'generate_character_scene_image', side_effect=RuntimeError('first pass failed')),
              patch.object(self.cc, '_generate_identity_image') as second):
            with self.assertRaisesRegex(RuntimeError, 'first pass failed'):
                self.run_job()
        second.assert_not_called()
        self.promote.assert_not_called()
        self.video.assert_not_called()
        self.client.post.assert_not_called()

    def test_without_scene_keeps_existing_composition_path(self):
        self.job['payload'].pop('scene_reference_image')
        with patch.object(self.cc, 'generate_two_character_scene_image') as sequential:
            self.run_job()
        sequential.assert_not_called()
        self.legacy.assert_called_once()
        self.promote.assert_called_once_with('legacy.png', subfolder='')

    def test_continuation_uses_frame_without_recomposing_scene(self):
        self.job['payload']['continuation_relative_path'] = 'original.mp4'
        with (patch.object(worker, 'extract_video_continuation_frame', return_value=b'frame'),
              patch.object(self.cc, 'upload_bytes_to_input', return_value='frame.png'),
              patch.object(self.cc, 'generate_two_character_scene_image') as sequential):
            self.run_job()
        sequential.assert_not_called()
        self.legacy.assert_not_called()
        self.promote.assert_not_called()
        self.assertEqual(self.video.call_args.args[2], 'frame.png')
