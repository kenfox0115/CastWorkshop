"""No GPU calls: inspect real workflow bindings and mock transport faults."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import requests
os.environ.setdefault("COMFY_URL", "http://127.0.0.1:1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import comfy_client as cc

class MediaTests(unittest.TestCase):
    def test_silent_multichar_video_disconnects_audio_references(self):
        response=Mock();response.json.return_value={"p":{"status":{"status_str":"success"},
            "outputs":{"92":{"videos":[{"filename":"silent.mp4"}]}}}}
        with patch.object(cc,"submit_prompt",return_value="p") as submit, \
             patch.object(cc.requests,"get",return_value=response), \
             patch.object(cc,"fetch_image_bytes",return_value=b"video"), \
             patch.object(cc,"free_vram"):
            result=cc.generate_multichar_voice_reference_video(
                "a.png","b.png","scene.png","a.flac","b.flac","silent prompt",
                workflow_filename="presets/minimax_h3_2char_continue_widescreen.json",
                use_audio_references=False)
        graph=submit.call_args.args[0]
        self.assertEqual(result,b"video")
        self.assertNotIn("ref_audios.ref_audio_0",graph["136"]["inputs"])
        self.assertNotIn("ref_audios.ref_audio_1",graph["136"]["inputs"])
        self.assertNotIn("141",graph)
        self.assertNotIn("146",graph)

    def test_both_multichar_graphs_bind_images_and_audio(self):
        for aspect in ("vertical", "widescreen"):
            path = Path(cc.__file__).parent / "workflows/presets" / f"minimax_h3_2char_ref2v_{aspect}.json"
            graph = json.loads(path.read_text(encoding="utf-8"))
            inputs = graph["136"]["inputs"]
            for key, node in {"ref_images.ref_image_0":"137", "ref_images.ref_image_1":"143",
                              "ref_images.ref_image_2":"145", "ref_audios.ref_audio_0":"141",
                              "ref_audios.ref_audio_1":"146"}.items():
                self.assertEqual(inputs[key], [node, 0])
            self.assertFalse(any("sageattention" in n.get("class_type", "").lower() for n in graph.values()))

    def test_single_speaker_uses_only_that_characters_audio(self):
        response=Mock();response.json.return_value={"p":{"status":{"status_str":"success"},
            "outputs":{"92":{"videos":[{"filename":"one-speaker.mp4"}]}}}}
        with patch.object(cc,"submit_prompt",return_value="p") as submit, \
             patch.object(cc.requests,"get",return_value=response), \
             patch.object(cc,"fetch_image_bytes",return_value=b"video"), \
             patch.object(cc,"free_vram"):
            cc.generate_multichar_voice_reference_video(
                "a.png","b.png","scene.png","a.flac","b.flac","prompt",
                audio_reference_speakers=["b"])
        graph=submit.call_args.args[0]
        self.assertEqual(graph["141"]["inputs"]["audio"], "b.flac")
        self.assertEqual(graph["136"]["inputs"]["ref_audios.ref_audio_0"], ["141",0])
        self.assertNotIn("ref_audios.ref_audio_1",graph["136"]["inputs"])
        self.assertNotIn("146",graph)

    def test_audio_nodes_follow_first_speaker_order(self):
        response=Mock();response.json.return_value={"p":{"status":{"status_str":"success"},
            "outputs":{"92":{"videos":[{"filename":"ordered.mp4"}]}}}}
        with patch.object(cc,"submit_prompt",return_value="p") as submit, \
             patch.object(cc.requests,"get",return_value=response), \
             patch.object(cc,"fetch_image_bytes",return_value=b"video"), \
             patch.object(cc,"free_vram"):
            cc.generate_multichar_voice_reference_video(
                "a.png","b.png","scene.png","a.flac","b.flac","prompt",
                audio_reference_speakers=["b","a"])
        graph=submit.call_args.args[0]
        self.assertEqual(graph["141"]["inputs"]["audio"], "b.flac")
        self.assertEqual(graph["146"]["inputs"]["audio"], "a.flac")

    def test_image_loader_submits_real_workflow(self):
        with patch.object(cc, "submit_prompt", return_value="p") as submit:
            self.assertEqual(cc.submit_workflow("a neutral scene"), "p")
            self.assertEqual(submit.call_args.args[0]["30:19"]["inputs"]["value"], "a neutral scene")

    def test_look_candidates_force_sfw_graph_while_chat_is_nsfw(self):
        info = {"filename": "candidate.png", "subfolder": "", "type": "output"}
        with patch.object(cc, "_content_mode", return_value="nsfw"), \
             patch.object(cc, "submit_prompt", return_value="p") as submit, \
             patch.object(cc, "wait_for_result", return_value=info), \
             patch.object(cc, "fetch_image_bytes", return_value=b"image"), \
             patch.object(cc, "free_vram"):
            cc.generate_look_variants(["a realistic adult portrait"])
        sent = submit.call_args.args[0]
        self.assertEqual(
            sent["30:10"]["inputs"]["unet_name"],
            "krea2_turbo_fp8_scaled.safetensors",
        )
        banned = ("filterbypass", "knpv", "gonzo", "gonzolomo", "uncensored", "realism_engine")
        for node in sent.values():
            if str(node.get("class_type", "")).lower() not in ("loraloader", "loraloadermodelonly"):
                continue
            inputs = node.get("inputs", {})
            name = str(inputs.get("lora_name", "")).lower()
            if any(token in name for token in banned):
                self.assertEqual(inputs.get("strength_model"), 0.0)

    def test_multichar_image_preserves_output_subfolder(self):
        info = {"filename":"scene.png", "subfolder":"nested", "type":"output"}
        with patch.object(cc, "submit_prompt", return_value="p"), patch.object(cc, "wait_for_images", return_value=info), patch.object(cc, "free_vram"):
            result = cc.generate_two_char_edit_image("a.png", "b.png", "two people outdoors")
            self.assertEqual(result, info)

    def test_all_multichar_image_variants_bind_both_references(self):
        for (aspect, mode), filename in cc._NEWDEFAULTS_2CHAR_REF.items():
            with self.subTest(aspect=aspect, mode=mode), \
                 patch.object(cc, "_aspect_mode", return_value=aspect), \
                 patch.object(cc, "_content_mode", return_value=mode), \
                 patch.object(cc, "submit_prompt", return_value="binding-test") as submit, \
                 patch.object(cc, "wait_for_images", return_value={"filename": "scene.png"}), \
                 patch.object(cc, "free_vram"):
                cc.generate_two_char_edit_image("character-a.png", "character-b.png", "Two people walking")
                sent = submit.call_args.args[0]
                original = json.loads(Path(cc.resolve_workflow_path(filename)).read_text(encoding="utf-8"))
                self.assertEqual(sent["72"]["inputs"]["image"], "character-a.png")
                second = "90"
                self.assertEqual(sent[second]["inputs"]["image"], "character-b.png")
                # Preserve all graph edges, including the order of image references.
                for node_id, node in original.items():
                    for key, value in node.get("inputs", {}).items():
                        if isinstance(value, list):
                            if node['class_type'] == 'SaveImage' and key == 'images':
                                unload_id = sent[node_id]['inputs'][key][0]
                                self.assertEqual(sent[unload_id]['class_type'], 'UnloadAllModels')
                                self.assertEqual(sent[unload_id]['inputs']['value'], value)
                            else:
                                self.assertEqual(sent[node_id]["inputs"][key], value)

    def test_all_scene_variants_bind_two_identities_and_environment(self):
        for aspect in ('vertical', 'cinematic'):
            for mode in ('sfw', 'nsfw'):
                with self.subTest(aspect=aspect, mode=mode), patch.object(cc, '_aspect_mode', return_value=aspect), patch.object(cc, '_content_mode', return_value=mode), patch.object(cc, 'generate_two_character_scene_image', return_value={'filename': 'scene.png'}) as generate:
                    cc.generate_two_char_edit_image('character-a.png', 'character-b.png', 'Two people walking', scene_reference='empty-scene.png')
                    self.assertEqual(generate.call_args.args[:3], ('character-a.png', 'character-b.png', 'empty-scene.png'))
                    self.assertEqual(generate.call_args.args[-1], 'Two people walking')

    def test_moss_sampling_parameters_are_preserved(self):
        original = json.loads((Path(cc.__file__).parent / "workflows/TTS_Suite_MOSS_Unload.json").read_text(encoding="utf-8"))
        response = Mock()
        response.json.return_value = {"p":{"outputs":{"11":{"audio":[{"filename":"voice.wav","subfolder":"audio"}]}},"status":{"status_str":"success"}}}
        with patch.object(cc,"submit_prompt",return_value="p") as submit, patch.object(cc.requests,"get",return_value=response), patch.object(cc,"fetch_image_bytes",return_value=b"audio"), patch.object(cc,"free_vram"):
            result = cc.generate_voice_candidates_moss("Test line", n=1, voice_description="calm voice")
            sent = submit.call_args.args[0]
            self.assertEqual(sent["6"], original["6"])
            for key, value in original["7"]["inputs"].items():
                if key not in ("reference_text","voice_instruction","seed"):
                    self.assertEqual(sent["7"]["inputs"][key], value)
            self.assertEqual(result[0][0], b"audio")

    def test_selected_moss_voice_regenerates_only_requested_reference_text(self):
        source = Mock()
        source.json.return_value = {
            "candidate": {
                "outputs": {"11": {"audio": [{"filename": "preview.flac"}]}},
                "prompt": [None, None, {"7": {"inputs": {
                    "seed": 42, "voice_instruction": "A natural adult voice",
                    "reference_text": "Hi, I'm Avery. Preview text.",
                }}}],
            }
        }
        rendered = Mock()
        rendered.json.return_value = {
            "new": {"outputs": {"11": {"audio": [{
                "filename": "clean.flac", "subfolder": "audio"
            }]}}}
        }
        with patch.object(cc.requests, "get", side_effect=[source, rendered]), \
             patch.object(cc, "submit_prompt", return_value="new") as submit, \
             patch.object(cc, "promote_output_to_input", return_value="clean.flac"), \
             patch.object(cc, "free_vram"):
            result = cc.generate_moss_line_from_saved_voice(
                "preview.flac", "A neutral reference passage."
            )
        self.assertEqual(result, "clean.flac")
        designer = submit.call_args.args[0]["7"]["inputs"]
        self.assertEqual(designer["reference_text"], "A neutral reference passage.")
        self.assertNotIn("Avery", designer["reference_text"])
        self.assertEqual(designer["seed"], 42)

    def test_submission_timeout_is_not_retried(self):
        idle = Mock()
        idle.json.return_value = {"queue_running":[], "queue_pending":[]}
        with patch.object(cc.requests, "get", return_value=idle), patch.object(cc.requests, "post", side_effect=requests.Timeout) as post:
            with self.assertRaises(requests.Timeout):
                cc.submit_prompt({})
            self.assertEqual(post.call_count, 1)

    def test_busy_engine_does_not_receive_another_prompt(self):
        busy = Mock()
        busy.json.return_value = {"queue_running":[[1,"existing"]]}
        with patch.object(cc.requests, "get", return_value=busy), patch.object(cc.requests, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "still has work"):
                cc.submit_prompt({})
            post.assert_not_called()

    def test_gpu_error_is_preserved(self):
        response = Mock()
        response.json.return_value = {"p": {"status":{"status_str":"error","messages":["CUDA out of memory"]},"outputs":{}}}
        with patch.object(cc.requests, "get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "CUDA out of memory"):
                cc.wait_for_images("p", "29")

    def test_transient_history_error_does_not_resubmit(self):
        response = Mock()
        response.json.return_value = {"p": {"status":{"status_str":"success"},"outputs":{"29":{"images":[{"filename":"image.png","subfolder":"scene"}]}}}}
        with patch.object(cc.requests, "get", side_effect=[requests.Timeout, response]), patch.object(cc.time,"sleep"):
            self.assertEqual(cc.wait_for_images("p", "29")["subfolder"], "scene")

if __name__ == "__main__":
    unittest.main()
