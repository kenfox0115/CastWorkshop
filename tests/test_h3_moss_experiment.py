import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "h3_moss_experiment", ROOT / "scripts" / "experiment_h3_moss_dialogue.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class H3MossExperimentTests(unittest.TestCase):
    def test_partial_copy_prompt_has_one_exact_transcript(self):
        line = "aww, which one is yours"
        scene = f'Frankie points to the dogs and says "{line}" sweetly.'
        prompt = MODULE.two_character_prompt("John", "Frankie", "Frankie", line, scene, True)

        self.assertIn("<Audio 1>: partially_copy", prompt)
        self.assertIn("<Subject 1> is John", prompt)
        self.assertIn("<Subject 2> is Frankie", prompt)
        self.assertIn("<Picture 3> is the target composition", prompt)
        self.assertEqual(prompt.count(f"<d>[English] {line}</d>"), 1)
        self.assertNotIn(f'"{line}"', prompt)
        self.assertIn("overall_soundscape:", prompt)
        self.assertIn("non_diegetic_music: N/A", prompt)


if __name__ == "__main__":
    unittest.main()
