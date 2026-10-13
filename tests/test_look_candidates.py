"""Offline contracts for structured character look planning."""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "worker"))
os.environ.setdefault("VPS_URL", "http://127.0.0.1:1")
from worker import worker


class LookCandidateTests(unittest.TestCase):
    def test_uses_schema_object_and_reads_candidates(self):
        response = '{"candidates":["first adult", "second adult", "third adult"]}'
        with patch.object(worker, "call_ollama", return_value=response) as call:
            candidates, stylized = worker.generate_look_variants_llm("an adult woman")
        self.assertEqual([c.split(" Subtle human expression:")[0] for c in candidates], ["first adult", "second adult", "third adult"])
        self.assertTrue(all("attentive eyes" in c for c in candidates))
        self.assertFalse(stylized)
        schema = call.call_args.kwargs["json_mode"]
        self.assertEqual(schema["required"], ["candidates"])
        self.assertEqual(schema["properties"]["candidates"]["minItems"], 3)

    def test_retries_one_invalid_response(self):
        valid = '{"candidates":["first adult", "second adult", "third adult"]}'
        with patch.object(worker, "call_ollama", side_effect=["not json", valid]) as call:
            candidates, _ = worker.generate_look_variants_llm("an adult woman")
        self.assertEqual(len(candidates), 3)
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
