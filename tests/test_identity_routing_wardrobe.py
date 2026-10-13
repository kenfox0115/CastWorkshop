"""Regression coverage for native routing and final wardrobe precedence."""
import copy
import json
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import comfy_client as cc
from worker.image_prompts import build_image_prompt, resolve_wardrobe_state


class IdentityRoutingWardrobeTests(TestCase):
    def test_final_states_win_over_source_outfits(self):
        cases = [
            ('She is wearing fancy lingerie, her top now fully removed.', None, 'topless', 'wearing fancy lingerie'),
            ('She is wearing a blue dress at a bar.', 'fully nude', 'fully nude', 'blue dress'),
            ('She is wearing fancy lingerie at a bar.', 'red evening gown', 'red evening gown', 'fancy lingerie'),
            ('She wears a black dress. Remove the dress so she is nude.', None, 'nude', 'black dress'),
            ('She is wearing a business suit, changed into a red evening gown.', None, 'red evening gown', 'business suit'),
        ]
        for scene, request, expected, absent in cases:
            with self.subTest(scene=scene):
                prompt = build_image_prompt(scene, latest_request=request)
                self.assertIn(expected, prompt)
                self.assertNotIn(absent, prompt)
                for identity in ('facial identity', 'structure', 'hair', 'skin tone', 'build'):
                    self.assertIn(identity, prompt)

    def test_unchanged_outfit_and_nonwardrobe_actions_survive(self):
        scene = 'She is wearing a blue dress, standing at a bar.'
        self.assertEqual(resolve_wardrobe_state(scene, 'standing at a bar'), scene)

    def test_latest_request_overrides_planned_state(self):
        prompt = build_image_prompt('She is topless beside a window.', latest_request='Wear a red evening gown')
        self.assertIn('red evening gown', prompt)
        self.assertNotIn('topless', prompt)
        self.assertIn('beside a window', prompt)

    def test_native_routing_for_legacy_single_identity_override(self):
        for mode in ('sfw', 'nsfw'):
            with (patch.object(cc, '_content_mode', return_value=mode),
                  patch.object(cc, '_aspect_mode', return_value='vertical'),
                  patch.object(cc, 'submit_prompt', return_value='native') as submit,
                  patch.object(cc, 'wait_for_images', return_value={'filename':'out.png'}),
                  patch.object(cc, 'fetch_image_bytes', return_value=b'image'),
                  patch.object(cc, 'free_vram')):
                cc.generate_edit_image('identity.png', 'Stand at a bar', 'Krea2_turbo_SNOFS.json')
                graph = submit.call_args.args[0]
                kinds = {n['class_type'] for n in graph.values()}
                self.assertIn('Krea2EditModelPatch', kinds)
                self.assertIn('Krea2EditGroundedEncode', kinds)
                self.assertNotIn('TextEncodeQwenImageEditPlus', kinds)
                self.assertNotIn('FluxKontextMultiReferenceLatentMethod', kinds)
                self.assertEqual(graph['111']['inputs']['strength_model'], int(mode=='nsfw'))
                self.assertEqual(graph['72']['inputs']['image'], 'identity.png')

    def test_text_to_image_remains_on_existing_graph(self):
        filename = 'k2_txt2img_api.json'
        expected = json.loads(Path(cc.resolve_workflow_path(filename)).read_text())
        with (patch.object(cc, '_content_mode', return_value='sfw'),
              patch.object(cc, 'submit_prompt', return_value='txt') as submit):
            cc.submit_workflow('A blue vase', workflow_filename=filename)
        self.assertEqual({n['class_type'] for n in submit.call_args.args[0].values()},
                         {n['class_type'] for n in expected.values()} | {'UnloadAllModels'})

    def test_adult_enhancer_preserves_final_state_sfw_is_unchanged(self):
        graph = json.loads(Path(cc.resolve_workflow_path('Krea2_turbo_SNOFS.json')).read_text())
        original = copy.deepcopy(graph)
        cc.configure_prompt_enhancement(graph, False)
        self.assertEqual(graph, original)
        cc.configure_prompt_enhancement(graph, True)
        self.assertNotIn('Assume clothing covers genitals', str(graph))
        self.assertIn('Preserve the requested final wardrobe or nudity state', str(graph))

    def test_scene_role_mapping_survives_wardrobe_resolution(self):
        prompt = build_image_prompt('She is wearing lingerie.', ((2, 'Ava'),), 1, latest_request='topless')
        self.assertIn('image 1 for the environment', prompt)
        self.assertIn("image 2 for Ava's facial identity", prompt)
        self.assertIn('topless', prompt)
        self.assertNotIn('wearing lingerie', prompt)
