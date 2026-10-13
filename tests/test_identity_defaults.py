import json
from pathlib import Path
import unittest
import sys
import os
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'worker')]
os.environ.setdefault('VPS_URL', 'http://127.0.0.1:1')
os.environ.setdefault('COMFY_URL', 'http://127.0.0.1:1')
from worker import comfy_client as cc
from worker import worker


class IdentityDefaultTests(unittest.TestCase):
    def test_all_image_workflows_share_the_setup_model_pair(self):
        diffusion = 'krea2_turbo_fp8_scaled.safetensors'
        encoder = 'qwen3vl_4b_fp8_scaled.safetensors'
        for path in (ROOT / 'worker/workflows').rglob('*.json'):
            graph = json.loads(path.read_text(encoding='utf-8'))
            for node in graph.values():
                inputs = node.get('inputs', {})
                if node['class_type'] == 'UNETLoader' and 'krea2' in inputs.get('unet_name', '').lower():
                    with self.subTest(workflow=path.name):
                        self.assertEqual(inputs['unet_name'], diffusion)
                if node['class_type'] == 'CLIPLoader' and inputs.get('type') == 'krea2':
                    with self.subTest(workflow=path.name):
                        self.assertEqual(inputs['clip_name'], encoder)
        assets = json.loads((ROOT / 'config/setup_dependencies.json').read_text(encoding='utf-8'))['assets']
        names = {asset['name'] for asset in assets}
        self.assertIn(diffusion, names)
        self.assertIn(encoder, names)
        self.assertNotIn('krea2_turbo_int8_convrot.safetensors', names)
        self.assertNotIn('Huihui-Qwen3-VL-4B-Instruct-abliterated-fp8_scaled.safetensors', names)

    def test_candidate_batch_submits_shared_models_and_keeps_portrait_framing(self):
        with (patch.object(cc, 'submit_prompt', side_effect=['a', 'b']) as submit,
              patch.object(cc, 'wait_for_result', return_value={'filename': 'candidate.png'}),
              patch.object(cc, 'fetch_image_bytes', return_value=b'image'),
              patch.object(cc, 'free_vram'),
              patch.object(cc, '_content_mode', return_value='nsfw')):
            result = cc.generate_look_variants(['first appearance', 'second appearance'])
        self.assertEqual(len(result), 2)
        for call in submit.call_args_list:
            graph = call.args[0]
            self.assertEqual(graph['30:10']['inputs']['unet_name'], 'krea2_turbo_fp8_scaled.safetensors')
            self.assertEqual(graph['30:11']['inputs']['clip_name'], 'qwen3vl_4b_fp8_scaled.safetensors')
            self.assertEqual(graph['49']['inputs']['aspect_ratio'], '3:4 (Portrait Standard)')

    def test_mode_aspect_reference_matrix_uses_approved_clownshark_baseline(self):
        for aspect in ('vertical','cinematic'):
            for mode in ('sfw','nsfw'):
                for refs in ((),('a.png',),('a.png','b.png')):
                    with (
                        self.subTest(aspect=aspect,mode=mode,refs=refs),
                        patch.object(cc,'_aspect_mode',return_value=aspect),
                        patch.object(cc,'_content_mode',return_value=mode),
                        patch.object(cc,'submit_prompt',return_value='p') as submit,
                        patch.object(cc,'wait_for_images',return_value={'filename':'image.png'}),
                        patch.object(cc,'free_vram'),
                    ):
                        filename=cc._NEWDEFAULTS_2CHAR_REF[(aspect,mode)]
                        self.assertEqual(worker._NEWDEFAULTS_REF[(aspect,mode)],filename)
                        cc._generate_identity_image(filename,'requested scene',*refs)
                        sent=submit.call_args.args[0]
                        self.assertEqual(sent['84']['inputs']['prompt'],'requested scene')
                        self.assertEqual(sent['111']['inputs']['strength_model'],int(mode=='nsfw'))
                        self.assertEqual(sent['71']['inputs']['strength_model'],1)
                        self.assertEqual(filename, 'krea2_identity_edit_clownshark.json')
                        self.assertNotIn('53', sent)
                        self.assertEqual(sent['112']['class_type'], 'ClownsharKSampler_Beta')
                        self.assertEqual(sent['112']['inputs']['steps'],8)
                        self.assertEqual(sent['112']['inputs']['cfg'],1)
                        self.assertEqual(sent['112']['inputs']['sampler_name'],'exponential/ddim')
                        self.assertEqual(sent['112']['inputs']['scheduler'],'beta57')
                        self.assertEqual(sent['112']['inputs']['eta'],0.5)
                        self.assertEqual(sent['112']['inputs']['steps_to_run'],-1)
                        self.assertEqual(sent['112']['inputs']['denoise'],1)
                        self.assertEqual(sent['112']['inputs']['sampler_mode'],'standard')
                        self.assertFalse(sent['112']['inputs']['bongmath'])
                        self.assertEqual(sent['84']['inputs']['grounding_px'],512)
                        self.assertEqual(sent['85']['inputs']['grounding_px'],768)
                        self.assertEqual(sent['55']['inputs']['unet_name'],'krea2_turbo_fp8_scaled.safetensors')
                        self.assertEqual(sent['56']['inputs']['clip_name'],'qwen3vl_4b_fp8_scaled.safetensors')
                        if refs:
                            self.assertEqual(sent['79']['inputs']['ref_boost'],1.5)
                            self.assertEqual(sent['79']['inputs']['ref_boost_a'],1.5)
                        self.assertEqual(sent['83']['inputs']['aspect_ratio'],'16:9 (Widescreen)' if aspect=='cinematic' else '9:16 (Portrait Widescreen)')
                        unload_id = sent['29']['inputs']['images'][0]
                        self.assertEqual(sent[unload_id]['class_type'],'UnloadAllModels')
                        self.assertEqual(sent[unload_id]['inputs']['value'],['54',0])
                        self.assertEqual(sent['54']['inputs']['samples'],['112',0])
                        self.assertEqual('72' in sent,bool(refs))
                        self.assertEqual('90' in sent,len(refs)==2)
                        for node in sent.values():
                            for value in node.get('inputs',{}).values():
                                if isinstance(value,list) and len(value)==2 and isinstance(value[1],int):
                                    self.assertIn(value[0],sent)
                        names=[node['inputs'].get('lora_name','') for node in sent.values()]
                        self.assertFalse(any(any(retired in name for retired in ('filterbypass','realism_engine','KNPV')) for name in names))

    def test_single_character_scene_uses_environment_first(self):
        with patch.object(cc,'_generate_identity_image',return_value={'filename':'scene.png'}) as generate, patch.object(cc,'prepare_identity_reference',return_value='isolated.png'), patch.object(cc,'fetch_image_bytes',return_value=b'image'):
            self.assertEqual(cc.generate_character_scene_image('character.png','environment.png','requested scene'),b'image')
        self.assertEqual(generate.call_args.args[2:],('environment.png','isolated.png'))

    def test_three_reference_default_uses_sequential_native_insertion(self):
        with patch.object(cc, 'generate_two_character_scene_image', return_value={'filename': 'scene.png'}) as generate:
            result = cc.generate_two_char_edit_image('a.png', 'b.png', 'requested action', scene_reference='scene.png')
        self.assertEqual(result, {'filename': 'scene.png'})
        self.assertEqual(generate.call_args.args, ('a.png', 'b.png', 'scene.png', 'the first character', 'the second character', 'requested action'))

    def test_queued_legacy_native_presets_resolve_to_one_approved_source(self):
        for filename in cc._LEGACY_IDENTITY_PRESETS:
            with self.subTest(filename=filename):
                self.assertEqual(cc.resolve_identity_workflow('presets/' + filename),
                                 cc.IDENTITY_WORKFLOW)

    def test_canonical_template_contains_no_example_scene_or_references(self):
        graph = json.loads(Path(cc.resolve_workflow_path(cc.IDENTITY_WORKFLOW)).read_text())
        self.assertEqual(graph['84']['inputs']['prompt'], '')
        self.assertEqual(graph['85']['inputs']['prompt'], '')
        self.assertEqual(graph['72']['inputs']['image'], '')
        self.assertEqual(graph['90']['inputs']['image'], '')

    def test_builder_preserves_every_supplied_sampler_setting_except_seed(self):
        original = json.loads(Path(cc.resolve_workflow_path(cc.IDENTITY_WORKFLOW)).read_text())
        with (patch.object(cc,'submit_prompt',return_value='source-truth') as submit,
              patch.object(cc,'wait_for_images',return_value={'filename':'out.png'}),
              patch.object(cc,'free_vram')):
            cc._generate_identity_image(cc.IDENTITY_WORKFLOW,'Final scene','a.png','b.png')
        actual = submit.call_args.args[0]['112']
        expected = original['112']
        expected['inputs']['seed'] = actual['inputs']['seed']
        self.assertEqual(actual, expected)

    def test_shared_template_selection_does_not_force_sfw_mode(self):
        from vps import app as api
        self.assertIsNone(api._infer_workflow_mode(cc.IDENTITY_WORKFLOW))
        self.assertIn(cc.IDENTITY_WORKFLOW, api.EXCLUDE_FROM_SCAN)


if __name__=='__main__': unittest.main()
