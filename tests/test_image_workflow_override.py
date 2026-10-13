import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from worker import comfy_client as cc


class ImageWorkflowOverrideTests(unittest.TestCase):
    def test_single_reference_override_keeps_model_and_sampler_with_two_or_three_references(self):
        filename = "Krea2_turbo_SNOFS.json"
        original = json.loads(Path(cc.resolve_workflow_path(filename)).read_text(encoding="utf-8"))
        for scene in (None, "environment.png"):
            with (
                self.subTest(scene=scene),
                patch.object(cc, "_content_mode", return_value="nsfw"),
                patch.object(cc, "_aspect_mode", return_value="vertical"),
                patch.object(cc, "prepare_identity_reference", side_effect=lambda name: "isolated-" + name),
                patch.object(cc, "submit_prompt", return_value="override-test") as submit,
                patch.object(cc, "wait_for_images", return_value={"filename": "output.png"}),
                patch.object(cc, "free_vram"),
            ):
                cc.generate_two_char_edit_image("a.png", "b.png", "Two characters", workflow_filename=filename, scene_reference=scene)
                sent = submit.call_args.args[0]
                self.assertEqual(sent["69"]["inputs"]["image"], scene or "a.png")
                self.assertEqual(sent["104"]["inputs"]["image"], "isolated-a.png" if scene else "b.png")
                self.assertEqual(sent["30:52"]["inputs"]["image2"], ["104", 0])
                if scene:
                    self.assertEqual(sent["150"]["inputs"]["image"], "isolated-b.png")
                    self.assertEqual(sent["30:52"]["inputs"]["image3"], ["150", 0])
                for node_id, node in original.items():
                    if node["class_type"] in ("UNETLoader", "CheckpointLoaderSimple", "CLIPLoader", "LoraLoader", "LoraLoaderModelOnly", "ClownsharKSampler_Beta"):
                        expected = copy.deepcopy(node)
                        if "seed" in expected["inputs"]:
                            expected["inputs"]["seed"] = sent[node_id]["inputs"]["seed"]
                        self.assertEqual(sent[node_id], expected)

    def test_incompatible_graph_fails_before_submission(self):
        with self.assertRaisesRegex(ValueError, "Image override"):
            cc._expand_image_override_references({"69": {"class_type": "LoadImage", "inputs": {}}})


if __name__ == "__main__":
    unittest.main()
