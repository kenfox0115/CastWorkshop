"""Offline contracts for concise image roles and expressive look candidates."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "worker"))
import os
os.environ.setdefault("VPS_URL", "http://127.0.0.1:1")
import unittest
from unittest.mock import patch
from worker.image_prompts import build_image_prompt, build_expression_instruction
from worker import worker
from worker import comfy_client


class ImagePromptTests(unittest.TestCase):
    def test_scene_identity_mapping_and_idempotent_assembly(self):
        prompt = build_image_prompt("Wear a green dress and sit by the window.", ((2, "Ava"),), 1)
        self.assertIn("image 1 for the environment", prompt)
        self.assertIn("image 2 for Ava's facial identity", prompt)
        self.assertEqual(build_image_prompt(prompt, ((2, "Ava"),), 1), prompt)
        self.assertEqual(prompt.count("facial identity"), 1)
        self.assertLess(len(prompt.split()), 80)

    def test_queued_legacy_header_does_not_copy_old_clothes(self):
        old = ("Picture 1 defines the character identity only. Picture 2 defines the environment only. "
               "This reference photo shows a woman in a red suit in a kitchen. "
               "Keep this exact same person. Only change the following: Wear a blue dress outdoors.")
        prompt = build_image_prompt(old, ((2, "Ava"),), 1)
        self.assertNotIn("red suit", prompt)
        self.assertNotIn("kitchen", prompt)
        self.assertNotIn("Picture", prompt)
        self.assertIn("Ava is wearing a blue dress outdoors", prompt)

    def test_explicit_state_change_is_preserved(self):
        action = "Remove the dress; instead be fully unclothed."
        self.assertIn("fully nude", build_image_prompt(action))
        self.assertNotIn("Remove the dress", build_image_prompt(action))
        with self.assertRaisesRegex(ValueError, "Conflicting clothing"):
            build_image_prompt("Wearing a dress and fully unclothed.")
        # Two people's different states must not be mistaken for a contradiction.
        build_image_prompt("Ava is wearing a dress; Ben is fully unclothed.", ((1, "Ava"), (2, "Ben")))

    def test_invalid_role_assignments_fail_clearly(self):
        with self.assertRaises(ValueError):
            build_image_prompt("Sit down", ((1, "Ava"),), 1)
        with self.assertRaises(ValueError):
            build_image_prompt("image 1 is the character", ((2, "Ava"),), 1)

    def test_explicit_expression_wins(self):
        self.assertEqual(build_expression_instruction("An adult with a neutral expression", "playful"), "")
        self.assertEqual(build_expression_instruction("An adult smiling broadly", "melancholy"), "")

    def test_personality_and_fallback(self):
        self.assertIn("teasing smile", build_expression_instruction("An adult with black hair", "playful"))
        self.assertIn("attentive eyes", build_expression_instruction("An adult with black hair"))
        mixed = build_expression_instruction("An adult", "confident but shy")
        self.assertIn("self-assured gaze", mixed)
        self.assertIn("reserved posture", mixed)
        self.assertNotIn("tentative smile", mixed)

    def test_look_planner_uses_personality_and_preserves_style(self):
        with patch.object(worker, "call_ollama", return_value='{"candidates":["adult illustrated portrait"]}') as call:
            candidates, stylized = worker.generate_look_variants_llm("An illustrated adult", 1, "introspective")
        self.assertTrue(stylized)
        self.assertIn("thoughtful gaze", candidates[0])
        self.assertIn("Match explicitly requested art styles", call.call_args.args[0])
        self.assertNotIn("ALL candidates are Caucasian", call.call_args.args[0])

    def test_real_scene_binding_matches_prompt_roles(self):
        with patch.object(comfy_client, "_generate_identity_image", return_value={}) as generate, \
             patch.object(comfy_client, "prepare_identity_reference", return_value="isolated.png"), \
             patch.object(comfy_client, "fetch_image_bytes", return_value=b"image"):
            comfy_client.generate_character_scene_image("identity.png", "scene.png", "Wear a coat")
        self.assertEqual(generate.call_args.args[2:], ("scene.png", "isolated.png"))
        self.assertIn("image 1 for the environment", generate.call_args.args[1])
        self.assertIn("image 2 for the character", generate.call_args.args[1])
