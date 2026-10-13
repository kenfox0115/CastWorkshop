import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_identity_defaults import cc
from worker.image_prompts import build_image_prompt, build_sequential_image_prompts


class IdentityFollowupTests(unittest.TestCase):
    metadata = {'Zara': {'concept': 'Tall muscular Russian woman early twenties'},
                'Molly': {'concept': 'Caucasian woman early twenties curly black hair'}}

    def prompt(self, scene, request=None, names=('Zara', 'Molly')):
        return build_image_prompt(scene, tuple(enumerate(names, 1)), latest_request=request,
                                  adult_mode=True, character_metadata=self.metadata)

    def test_approved_model_patch_matches_every_input(self):
        approved = json.loads((Path(cc.resolve_workflow_path(cc.IDENTITY_WORKFLOW))).read_text())
        expected = {'ref_boost': 1.5, 'ref_boost_a': 1.5, 'fit_mode': 'fit',
                    'model': ['111', 0], 'source_latent': ['73', 0], 'vae': ['57', 0],
                    'source_image': ['72', 0], 'source_image_b': ['90', 0],
                    'target_latent': ['82', 0]}
        self.assertEqual(approved['79']['inputs'], expected)
        for names, refs in ((('Zara','Molly'), ('zara.png','molly.png')),
                            (('Molly','Zara'), ('molly.png','zara.png'))):
            with patch.object(cc, 'submit_prompt', return_value='test') as submit, \
                 patch.object(cc, 'wait_for_images', return_value={}), patch.object(cc, 'free_vram'):
                prompt = self.prompt('Sitting at an outdoor cafe in los Angeles', names=names)
                cc._generate_identity_image(cc.IDENTITY_WORKFLOW, prompt, *refs)
                graph = submit.call_args.args[0]
                self.assertEqual(graph['79']['inputs'], expected)
                self.assertEqual(graph['112']['inputs']['latent_image'], expected['target_latent'])
                self.assertEqual(graph['72']['inputs']['image'], refs[0])
                self.assertEqual(graph['90']['inputs']['image'], refs[1])
                self.assertIn(f"Use image 1 for {names[0]}'s", prompt)
                self.assertIn(f"Use image 2 for {names[1]}'s", prompt)

    def test_missing_target_latent_rejected_before_submission(self):
        graph = json.loads(Path(cc.resolve_workflow_path(cc.IDENTITY_WORKFLOW)).read_text())
        del graph['79']['inputs']['target_latent']
        with patch.object(cc.json, 'load', return_value=graph), patch.object(cc, 'submit_prompt') as submit:
            with self.assertRaisesRegex(ValueError, 'target_latent'):
                cc._generate_identity_image(cc.IDENTITY_WORKFLOW, 'scene', 'a.png', 'b.png')
            submit.assert_not_called()

    def test_topless_expands_and_discards_source_upper_clothing(self):
        prompt = self.prompt('Zara wears a jacket. Molly wears a shirt.', 'topless')
        self.assertIn('bare upper bodies', prompt)
        self.assertIn('no bra, shirt, top, jacket, vest, or other upper-body garment', prompt)
        self.assertIn('Do not preserve conflicting upper-body clothing', prompt)
        self.assertNotIn('wears a jacket', prompt)
        self.assertNotIn('wears a shirt', prompt)
        self.assertNotIn('Final wardrobe:', prompt)

    def test_nude_expands_and_discards_all_source_clothing(self):
        prompt = self.prompt('Zara wears a jacket. Molly wears lingerie.', 'fully nude')
        self.assertIn('wear no clothing or lingerie', prompt)
        self.assertIn('Do not preserve any conflicting source garments', prompt)
        self.assertNotIn('wears lingerie', prompt)
        self.assertNotIn('wears a jacket', prompt)

    def test_lingerie_is_complete_outfit(self):
        prompt = self.prompt('Zara wears a jacket. Molly wears a dress.', 'lingerie')
        self.assertIn('requested lingerie is the complete final outfit', prompt)
        self.assertIn('Do not preserve jackets, outerwear, or conflicting source clothing', prompt)
        self.assertNotIn('wears a dress', prompt)
        self.assertNotIn('wears a jacket', prompt)

    def test_replacement_outfit_no_added_clothing(self):
        prompt = self.prompt('Zara wears a jacket. Molly wears a dress.', 'both are wearing blue shirts')
        self.assertIn('Zara is wearing a blue shirt', prompt)
        self.assertIn('Molly is wearing a blue shirt', prompt)
        self.assertIn('requested outfit completely replaces conflicting source wardrobe', prompt)
        for absent in ('wears a jacket', 'wears a dress', 'trousers', 'jeans', 'shoes'):
            self.assertNotIn(absent, prompt)

    def test_no_wardrobe_change_keeps_default_behavior(self):
        prompt = self.prompt('Zara wears a jacket. Molly wears a dress.')
        self.assertIn('Zara wears a jacket', prompt)
        self.assertIn('Molly wears a dress', prompt)
        self.assertNotIn('conflicting source wardrobe', prompt)
        self.assertNotIn('bare upper', prompt)

    def test_grammar_gender_guard_and_no_creative_additions(self):
        prompt = self.prompt('Sitting at an outdoor cafe in los Angeles')
        self.assertEqual(prompt.split('Requested edit: ')[1],
                         'Zara and Molly are sitting together at an outdoor caf\u00e9 in Los Angeles.')
        self.assertIn('exactly two adult women', prompt)
        self.assertIn('Keep their identities distinct. Do not blend or duplicate the characters.', prompt)
        self.assertNotIn('Russian', prompt)
        self.assertNotIn('muscular', prompt)
        self.assertNotIn('wearing', prompt)

    def test_sequential_final_pass_uses_gender_and_concrete_wardrobe(self):
        _, second = build_sequential_image_prompts('Standing at a bar', 'Zara', 'Molly',
                        latest_request='lingerie', adult_mode=True, character_metadata=self.metadata)
        self.assertIn('exactly two adult women', second)
        self.assertIn('requested lingerie is the complete final outfit', second)

    def test_explicit_gender_swaps_without_guessing_names(self):
        metadata = {'Zara': {'gender': 'female'}, 'James': {'gender': 'male'}}
        prompt = build_image_prompt('Standing together', ((1,'James'),(2,'Zara')),
                                    adult_mode=True, character_metadata=metadata)
        self.assertIn('James is a man; Zara is a woman.', prompt)
        self.assertIn('exactly two adult characters', prompt)
        unknown = build_image_prompt('Standing together', ((1,'Zara'),(2,'Molly')), adult_mode=True)
        self.assertIn('exactly two adult characters', unknown)
