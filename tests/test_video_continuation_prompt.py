"""Offline contracts for deterministic video continuation prompting."""
from pathlib import Path
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "worker"))
os.environ.setdefault("VPS_URL", "http://127.0.0.1:1")
from worker import worker as video_worker


class VideoContinuationPromptTests(unittest.TestCase):
    def test_silent_two_character_continuation_plan_passes_final_validation(self):
        source = "Barb leans down and continues the requested interaction with Marnie"
        authoritative_turns = video_worker._multichar_performance_hints(
            source, "Barb", "Marnie", [],
        )
        plan = video_worker._fallback_multichar_scene_plan(
            source,
            "Barb", "Marnie", {"a": None, "b": None}, [],
            authoritative_turns=authoritative_turns,
            continuation=True,
        )
        self.assertTrue(video_worker._validate_multichar_scene_plan(plan, authoritative_turns))
        body = video_worker.build_multichar_h3_video_prompt(
            "Barb", "adult", "Marnie", "adult", source, source,
            dialogue_turns=[], scene_plan=plan,
        )
        self.assertIn("supplied final frame", body)
        self.assertNotIn("<d>[English]", body)

    def test_facing_camera_and_scene_are_locked_without_invention(self):
        body = video_worker.build_h3_continuation_prompt(
            "turns to face the camera and raises her hand",
            "Naomi",
            spoken_line="Do you want to see?",
        )
        self.assertIn("turns to face the camera and raises her hand", body)
        self.assertNotIn("never turns away from the camera", body)
        self.assertIn("existing setting", body)
        self.assertIn("no cut", body)
        self.assertIn("minimal reverberation", body)
        self.assertIn("non_diegetic_music: N/A", body)
        self.assertEqual(body.count("Do you want to see?"), 1)
        self.assertNotIn("bedroom", body.lower())
        self.assertNotIn("garden", body.lower())

    def test_away_facing_request_is_not_reversed(self):
        body = video_worker.build_h3_continuation_prompt(
            "turns away from the camera and looks over her shoulder",
            "Naomi",
            has_audio=False,
        )
        self.assertIn("turns away from the camera and looks over her shoulder", body)
        self.assertNotIn("never turns away", body)

    def test_speech_instruction_is_removed_from_visible_motion(self):
        body = video_worker.build_h3_continuation_prompt(
            "She turns to face the camera while saying in a flirtatious tone",
            "Naomi",
            spoken_line="Hello",
        )
        self.assertIn("She turns to face the camera", body)
        self.assertNotIn("Naomi She", body)
        self.assertIn("flirtatious tone", body)

    def test_continuation_advances_without_replaying_or_resetting_clothing(self):
        body = video_worker.build_h3_continuation_prompt(
            "She lowers her hand",
            "Naomi",
            has_audio=False,
        )
        self.assertIn("Continue forward in time", body)
        self.assertIn("do not replay, reverse, or reenact", body)
        self.assertIn("unless the request explicitly changes that detail", body)
        self.assertNotIn("never restore, re-close, re-tie", body)

    def test_continuation_presets_anchor_the_first_frame(self):
        presets = ROOT / "worker" / "workflows" / "presets"
        for filename, image_node in (
            ("minimax_h3_continue_vertical.json", "137"),
            ("minimax_h3_continue_widescreen.json", "137"),
            ("minimax_h3_2char_continue_vertical.json", "145"),
            ("minimax_h3_2char_continue_widescreen.json", "145"),
        ):
            graph = json.loads((presets / filename).read_text(encoding="utf-8"))
            guide = graph["147"]
            self.assertEqual(guide["class_type"], "MiniMaxH3AddGuide")
            self.assertEqual(guide["inputs"]["frame_idx"], 0)
            self.assertEqual(guide["inputs"]["image"], [image_node, 0])
            self.assertEqual(graph["126"]["inputs"]["conditioning"], ["147", 0])
            if "2char" in filename:
                self.assertNotIn("audio", guide["inputs"])
            else:
                self.assertEqual(guide["inputs"]["audio"], ["148", 0])
                self.assertEqual(guide["inputs"]["audio_vae"], ["120", 0])
                self.assertEqual(graph["148"]["class_type"], "LoadAudio")


if __name__ == "__main__":
    unittest.main()
