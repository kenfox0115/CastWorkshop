"""Clean-install helper paths must not depend on the worker's cwd."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("COMFY_URL", "http://127.0.0.1:1")
from worker import comfy_client as cc
from scripts import comfy_setup


class IdentityHelperPathTests(unittest.TestCase):
    def test_relative_segmentation_model_is_project_relative(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "models/u2net_human_seg.onnx"
            model.parent.mkdir()
            model.touch()
            def isolate(command, **kwargs):
                self.assertEqual(Path(command[command.index("--model") + 1]), model)
                Path(command[command.index("--output") + 1]).write_bytes(b"isolated")
            with (patch.object(cc, "__file__", str(root / "worker/comfy_client.py")),
                  patch.dict(os.environ, {"IDENTITY_SEGMENTATION_MODEL": "./models/u2net_human_seg.onnx"}),
                  patch.object(cc, "_comfy_python", return_value="helper-python"),
                  patch.object(cc.requests, "get", return_value=Mock(content=b"portrait")),
                  patch.object(cc.subprocess, "run", side_effect=isolate),
                  patch.object(cc, "upload_bytes_to_input", return_value="isolated.png") as upload):
                self.assertEqual(cc.prepare_identity_reference("portrait.png"), "isolated.png")
                self.assertEqual(upload.call_args.args[0], b"isolated")

    def test_runtime_detects_portable_python_using_setup_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            comfy = root / "portable/ComfyUI"
            comfy.mkdir(parents=True)
            python = root / "portable/python_embeded/python.exe"
            python.parent.mkdir()
            python.touch()
            with (patch.dict(os.environ, {"COMFY_PYTHON": "", "COMFYUI_PATH": str(comfy)}),
                  patch.object(comfy_setup.subprocess, "run", return_value=Mock(returncode=0))):
                self.assertEqual(Path(cc._comfy_python()), python.resolve())

    def test_explicit_relative_python_is_project_relative(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "helper/python"
            python.parent.mkdir()
            python.touch()
            with (patch.object(cc, "__file__", str(root / "worker/comfy_client.py")),
                  patch.dict(os.environ, {"COMFY_PYTHON": "helper/python"})):
                self.assertEqual(Path(cc._comfy_python()), python)

    def test_invalid_explicit_python_does_not_choose_another_environment(self):
        with patch.dict(os.environ, {"COMFY_PYTHON": "missing/helper-python"}):
            self.assertEqual(cc._comfy_python(), "")
