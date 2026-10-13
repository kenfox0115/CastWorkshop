"""Behavioral contract replacing the retired, incorrect movement regex tests."""
import unittest
from test_video_prompt_contract import w

class MovementFidelityTests(unittest.TestCase):
    def test_reversed_rewrite_is_rejected(self):
        source = "Alex walks away from the doorway."
        self.assertEqual(w.enforce_movement_fidelity(source, "Alex enters the doorway."), source)

    def test_opening_frame_precedes_action(self):
        result = w.enhance_scene("Alex opens an umbrella, then sits down.", "Alex")
        self.assertIn("immediately before the first requested action", result)
        self.assertIn("Alex opens an umbrella, then sits down.", result)
