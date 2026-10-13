"""Chat prompt repair preserves intent and keeps pipeline errors internal."""
import json
import unittest
from unittest.mock import Mock, patch
from worker.image_prompts import build_image_prompt, compose_chat_media_scene
from worker.conversation import respond


class ChatPromptRepairTests(unittest.TestCase):
    def test_latest_outfit_wins_over_reply_and_old_scene(self):
        scene = compose_chat_media_scene("Fully nude and wearing a blue dress, seated in the library.",
                                        "Wear a red dress", "I am fully nude.")
        self.assertIn("wearing a red dress", scene)
        self.assertNotIn("fully nude", scene)
        self.assertNotIn("blue dress", scene)
        self.assertIn("seated in the library", scene)
        build_image_prompt(scene)

    def test_completed_reply_wins_over_generated_stale_outfit(self):
        scene = compose_chat_media_scene("Wearing a blue dress, seated in the library.",
                                        "Show me", "I am fully nude.")
        self.assertIn("fully nude", scene)
        self.assertNotIn("blue dress", scene)
        self.assertIn("seated in the library", scene)

    def test_conflicting_scene_is_resolved_without_weakening_manual_check(self):
        scene = "Fully nude and wearing a dress, seated beside the window."
        with self.assertRaisesRegex(ValueError, "Conflicting clothing"):
            build_image_prompt(scene)
        repaired = compose_chat_media_scene(scene)
        self.assertNotIn("wearing a dress", repaired)
        self.assertIn("seated beside the window", repaired)
        build_image_prompt(repaired)

    def test_application_reference_roles_replace_authored_roles(self):
        scene = compose_chat_media_scene("Image 1 is the environment. The character sits in a library.")
        self.assertNotIn("Image 1 is the environment", scene)
        self.assertIn("sits in a library", scene)
        build_image_prompt(scene)

    def plan(self, scene, kind="image"):
        return json.dumps({"response_type": kind, "scene": scene,
                           "visible_subjects": ["character"], "video_line": None})

    def test_validation_error_replans_without_redrafting_reply(self):
        call = Mock(side_effect=["Here you go.", self.plan("Sitting in the library."),
                                self.plan("Seated in the library.")])
        from worker.image_prompts import compose_chat_media_scene as compose
        count = 0
        def reject_once(*args):
            nonlocal count
            count += 1
            if count == 1:
                raise ValueError("invalid generated scene")
            return compose(*args)
        with patch("worker.conversation.compose_chat_media_scene", side_effect=reject_once), patch("worker.conversation.sfw_reply", side_effect=lambda reply,*args: (reply,True)), patch("worker.conversation.sfw_safe", return_value=True):
            result = respond({"message": "Send a photo"}, "test", call, Mock())
        self.assertEqual(result["text"], "Here you go.")
        self.assertEqual(result["response_type"], "image")
        self.assertEqual(call.call_count, 3)
        repair = json.loads(call.call_args_list[2].kwargs["messages"][1]["content"])
        self.assertEqual(repair["validation_error"], "invalid generated scene")

    def test_exhausted_composition_attempts_preserve_reply_and_request_internal_retry(self):
        call = Mock(side_effect=["Here you go."] + [self.plan("Sitting in the library.")] * 3)
        with patch("worker.conversation.compose_chat_media_scene", side_effect=ValueError("invalid generated scene")), patch("worker.conversation.sfw_reply", side_effect=lambda reply,*args: (reply,True)), patch("worker.conversation.sfw_safe", return_value=True):
            result = respond({"message": "Send a photo"}, "test", call, Mock())
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Here you go.")
        self.assertEqual(result["media_plan_repair"]["response_type"], "image")

    def test_planning_only_retry_reuses_saved_reply(self):
        call = Mock(return_value=self.plan("Sitting in the library."))
        with patch("worker.conversation.sfw_reply", side_effect=lambda reply,*args: (reply,True)), patch("worker.conversation.sfw_safe", return_value=True):
            result = respond({"message": "Send a photo", "drafted_reply": "Here you go.",
                              "media_plan_repair": {"response_type": "image", "error": "invalid scene"}}, "test", call, Mock())
        self.assertEqual(result["text"], "Here you go.")
        self.assertEqual(result["response_type"], "image")
        self.assertEqual(call.call_count, 1)

    def test_planner_outage_does_not_append_error_to_character_reply(self):
        call = Mock(side_effect=["Hello there.", TimeoutError("planner unavailable")])
        with patch("worker.conversation.sfw_reply", side_effect=lambda reply,*args: (reply,True)):
            result = respond({"message": "Hello"}, "test", call, Mock())
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Hello there.")

    def test_negative_reveal_request_wins_over_plan_and_reply(self):
        scene = compose_chat_media_scene("Fully nude and wearing a blue dress, seated in a library.",
                                        "Do not be nude", "I am fully nude.")
        self.assertNotIn("fully nude", scene)
        self.assertIn("wearing a blue dress", scene)
        self.assertIn("seated in a library", scene)

    def test_explicit_image_request_survives_planner_outage_as_internal_retry(self):
        call = Mock(side_effect=["Here you go.", TimeoutError("planner unavailable")])
        with patch("worker.conversation.sfw_reply", side_effect=lambda reply,*args: (reply,True)):
            result = respond({"message": "Send a photo"}, "test", call, Mock())
        self.assertEqual(result["text"], "Here you go.")
        self.assertEqual(result["media_plan_repair"]["response_type"], "image")

    def test_sfw_composition_cannot_reintroduce_rejected_reveal(self):
        for request in ("Fully nude", "Remove your top", "Topless"):
            scene = compose_chat_media_scene("Wearing a blue dress in the library.", request,
                                            "I am fully nude.", mode="sfw")
            self.assertIn("wearing a blue dress", scene.lower())
            self.assertNotIn("fully nude", scene)
            self.assertNotIn("topless", scene)
