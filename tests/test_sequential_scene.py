"""Offline sequential insertion contracts using actual native workflow bindings."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import comfy_client as cc
from worker.image_prompts import build_sequential_image_prompts


class SequentialSceneTests(unittest.TestCase):
    def test_job_687_authored_background_crew_does_not_conflict_with_cast_count(self):
        first, second = build_sequential_image_prompts(
            'Anika and Jordan wear futuristic military uniforms on the bridge while the rest of the crew works around them.',
            'Anika', 'Jordan')
        self.assertIn('only named principal character', first)
        self.assertIn('explicitly requested background crew', first)
        self.assertNotIn('Do not generate any additional person', first)
        self.assertNotIn('Jordan', first)
        self.assertIn('exactly two named principal characters: Anika and Jordan', second)
        self.assertIn('explicitly requested background crew', second)
        self.assertIn('crew works', first)
        self.assertIn('crew works', second)
        self.assertIn('futuristic military uniforms', first)
        self.assertIn('futuristic military uniforms', second)

    def test_negated_background_crew_keeps_two_character_restriction(self):
        first, second = build_sequential_image_prompts(
            'Anika and Jordan stand on the bridge without background crew.', 'Anika', 'Jordan')
        self.assertIn('Do not generate any additional person', first)
        self.assertNotIn('Include the explicitly requested background crew', first + second)

    def test_discussing_crew_does_not_add_background_people(self):
        first, second = build_sequential_image_prompts(
            'Anika and Jordan stand on the bridge and discuss background crew.', 'Anika', 'Jordan')
        self.assertIn('Do not generate any additional person', first)
        self.assertNotIn('Include the explicitly requested background crew', first + second)

    def test_both_passes_use_native_graph_with_ordered_references(self):
        for mode in ('sfw', 'nsfw'):
            for aspect in ('vertical', 'cinematic'):
                graphs = []
                updates = []
                def submit(graph):
                    graphs.append(graph)
                    return f'pass{len(graphs)}'
                with (patch.object(cc, '_content_mode', return_value=mode),
                      patch.object(cc, '_aspect_mode', return_value=aspect),
                      patch.object(cc, 'submit_prompt', side_effect=submit),
                      patch.object(cc, 'wait_for_images', side_effect=[{'filename':'first.png'}, {'filename':'final.png'}]),
                      patch.object(cc, 'prepare_identity_reference', side_effect=lambda name: f'isolated-{name}'),
                      patch.object(cc, 'fetch_image_bytes', return_value=b'pass-one'),
                      patch.object(cc, 'upload_image_bytes_to_input', return_value='intermediate.png') as upload,
                      patch.object(cc, 'free_vram') as free):
                    result = cc.generate_two_character_scene_image('a.png', 'b.png', 'scene.png',
                        'Ava', 'Ben', 'Ava and Ben sit beside each other.', progress=lambda **kw: updates.append(kw))
                self.assertEqual(result['filename'], 'final.png')
                self.assertEqual(len(graphs), 2)
                self.assertEqual(graphs[0]['72']['inputs']['image'], 'scene.png')
                self.assertEqual(graphs[0]['90']['inputs']['image'], 'isolated-a.png')
                self.assertEqual(graphs[1]['72']['inputs']['image'], 'intermediate.png')
                self.assertEqual(graphs[1]['90']['inputs']['image'], 'isolated-b.png')
                self.assertEqual(upload.call_args.args[0], b'pass-one')
                self.assertEqual(free.call_count, 2)
                self.assertEqual([u['image_pass'] for u in updates], [1, 2])
                self.assertEqual(updates[1]['intermediate_image']['filename'], 'intermediate.png')
                baseline = json.loads(Path(cc.resolve_workflow_path(cc._NEWDEFAULTS_2CHAR_REF[(aspect,mode)])).read_text())
                for graph in graphs:
                    self.assertNotIn('150', graph)
                    for field in ('steps', 'cfg', 'sampler_name', 'scheduler'):
                        self.assertEqual(graph['112']['inputs'][field], baseline['112']['inputs'][field])
                    self.assertEqual(graph['111']['inputs']['strength_model'], int(mode=='nsfw'))

    def test_stage_prompts_protect_existing_character(self):
        first, second = build_sequential_image_prompts('Ava and Ben sit across a table.', 'Ava', 'Ben')
        self.assertIn('Show exactly one character: Ava', first)
        self.assertNotIn('Ben', first)
        self.assertIn('open space beside Ava', first)
        self.assertIn('Image 1 is the established scene containing Ava', second)
        self.assertIn("Preserve Ava exactly as established", second)
        self.assertIn('Add Ben naturally', second)
        self.assertNotIn("Ava's facial identity", second)
        self.assertIn('Ben sits across a table.', second)

    def test_distinct_two_character_clothing_states_are_valid(self):
        build_sequential_image_prompts('Ava is wearing a dress; Ben is fully unclothed.', 'Ava', 'Ben')

    def test_first_pass_failure_does_not_submit_second(self):
        with patch.object(cc, 'generate_character_scene_image', side_effect=RuntimeError('failed')), \
             patch.object(cc, '_generate_identity_image') as second, \
             patch.object(cc, 'upload_image_bytes_to_input') as upload:
            with self.assertRaises(RuntimeError):
                cc.generate_two_character_scene_image('a', 'b', 'scene', 'Ava', 'Ben', 'stand together')
        second.assert_not_called()
        upload.assert_not_called()
