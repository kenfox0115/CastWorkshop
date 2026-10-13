import sys
import unittest
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.model_manifest import load_manifest, readiness


class ModelManifestTests(unittest.TestCase):
    def test_manifest_covers_every_model_named_by_bundled_workflows(self):
        root = Path(__file__).resolve().parents[1]
        referenced = set()
        suffixes = (".safetensors", ".gguf", ".onnx", ".pth", ".pt")
        for path in (root / "worker" / "workflows").rglob("*.json"):
            workflow = json.loads(path.read_text(encoding="utf-8"))
            for node in workflow.values():
                for value in node.get("inputs", {}).values():
                    if isinstance(value, str) and value.lower().endswith(suffixes):
                        referenced.add(value.replace("\\", "/").split("/")[-1])
        declared = {item["name"] for item in load_manifest()["models"]}
        self.assertEqual(referenced - declared, set())

    def test_readiness_matches_runtime_inventory_without_exposing_urls(self):
        def fetch(url):
            if url.endswith("/api/tags"):
                return {"models":[{"name":"custom-chat:latest"}]}
            return {"UNETLoader":{"input":{"required":{"unet_name":[["models/krea2_turbo_fp8_scaled.safetensors"]]}}}}

        report=readiness("http://secret-ollama","http://secret-comfy",ollama_model="custom-chat:latest",fetch=fetch)
        by_name={item["name"]:item["status"] for item in report["models"]}
        self.assertEqual(by_name["custom-chat:latest"],"present")
        self.assertEqual(by_name["krea2_turbo_fp8_scaled.safetensors"],"present")
        self.assertNotIn("secret",str(report))
        self.assertNotIn("gonzalomoKrea2_v40.safetensors", by_name)
        self.assertNotIn("KNPV3_1.safetensors", by_name)

    def test_unreachable_services_report_error_types_only(self):
        def fail(_url):
            raise OSError("private path and endpoint")

        report=readiness(fetch=fail)
        self.assertEqual(report["errors"],{"ollama":"OSError","comfyui":"OSError"})
        self.assertNotIn("private path",str(report))


if __name__ == "__main__":
    unittest.main()
