import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import error, request
import socket
import ssl
import zipfile

from scripts import comfy_setup


def asset(data=b"model data"):
    return {"name": "test.safetensors", "url": "https://huggingface.co/test/model/resolve/revision/test.safetensors",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def response(data, status=200, headers=None):
    result = io.BytesIO(data)
    result.status = status
    result.headers = headers or {"Content-Length": str(len(data))}
    return result


class ComfySetupTests(unittest.TestCase):
    def test_workflow_inventory_uses_utf8_on_legacy_windows_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / "worker/workflows"
            workflows.mkdir(parents=True)
            (workflows / "image.json").write_text(
                json.dumps({"1": {"class_type": "Example", "note": "\u008f\u009d"}}, ensure_ascii=False), encoding="utf-8")
            original_read = Path.read_text
            def legacy_read(path, **kwargs):
                return original_read(path, **({"encoding": "cp1252"} | kwargs))
            with patch.object(comfy_setup, "ROOT", root), patch.object(Path, "read_text", legacy_read):
                self.assertEqual(comfy_setup.required_classes(["images"]), {"Example"})

    def test_pip_report_uses_utf8_and_subprocess_decoding_is_explicit(self):
        calls = []
        def run(command, **kwargs):
            calls.append(kwargs)
            if "list" in command:
                return Mock(stdout=json.dumps([{"name": "torch", "version": "2.9.0"}, {"name": "torchaudio", "version": "2.9.0"}]), returncode=0)
            report = Path(command[command.index("--report") + 1])
            report.write_text(json.dumps({"install": [], "description": "\u008f\u009d"}, ensure_ascii=False), encoding="utf-8")
            return Mock(stdout="", stderr="", returncode=0)
        original_read = Path.read_text
        def legacy_read(path, **kwargs):
            return original_read(path, **({"encoding": "cp1252"} | kwargs))
        with patch.object(comfy_setup.subprocess, "run", side_effect=run), patch.object(Path, "read_text", legacy_read):
            comfy_setup.install_requirements("comfy-python", ["test-package"])
        self.assertTrue(all(call["encoding"] == "utf-8" for call in calls))

    def test_download_reports_dns_and_certificate_causes_without_urls(self):
        reasons = [(socket.gaierror(-2, "secret https://example.org?token=private"), "DNS lookup failed"),
                   (ssl.SSLCertVerificationError(1, "private"), "certificate verification failed")]
        for reason, message in reasons:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                opener = Mock()
                opener.open.side_effect = error.URLError(reason)
                with self.assertRaisesRegex(RuntimeError, message) as raised:
                    comfy_setup.download(asset(), Path(directory) / "file", opener)
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("https://", str(raised.exception))

    def test_manifest_covers_workflow_models_including_pinned_snofs(self):
        from scripts.model_manifest import load_manifest
        dependencies = comfy_setup.load_dependencies()
        declared = {a["name"] for a in dependencies["assets"]}
        excluded = {a["name"] for a in dependencies["excluded"]}
        workflows = {m["name"] for m in load_manifest()["models"] if m["engine"] == "comfyui" and m["category"] != "managed"}
        self.assertEqual(workflows - declared, excluded)
        self.assertIn("snofs_krea_v1_4.safetensors", declared)
        snofs = next(a for a in dependencies["assets"] if a["name"] == "snofs_krea_v1_4.safetensors")
        self.assertEqual(snofs["group"], "images")
        self.assertEqual(snofs["destination"], "loras/snofs_krea_v1_4.safetensors")
        self.assertIn("https://huggingface.co/Ashen3/SNOFS/resolve/", snofs["url"])
        self.assertEqual(len(snofs["sha256"]), 64)
        moss = [a for a in dependencies["assets"] if "/moss_tts/" in a["destination"]]
        self.assertTrue(moss)
        self.assertTrue(all(a["group"] == "video" for a in moss))

    def test_retired_loras_are_neither_downloaded_nor_required_by_workflows(self):
        from scripts.model_manifest import load_manifest
        retired = {"realism_engine_krea2_v2.safetensors", "krea2filterbypass3.safetensors"}
        self.assertFalse(retired.intersection(a["name"] for a in comfy_setup.load_dependencies()["assets"]))
        self.assertFalse(retired.intersection(m["name"] for m in load_manifest()["models"]))
        for path in (comfy_setup.ROOT / "worker/workflows").rglob("*.json"):
            graph = json.loads(path.read_text(encoding="utf-8"))
            for node in graph.values():
                with self.subTest(workflow=path.name):
                    self.assertNotIn(node.get("inputs", {}).get("lora_name"), retired)
                    for value in node.get("inputs", {}).values():
                        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                            self.assertIn(value[0], graph, "Dangling workflow connection")

    def test_download_verifies_and_atomically_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            opener = Mock()
            opener.open.return_value = response(b"model data")
            self.assertEqual(comfy_setup.download(asset(), target, opener), "installed")
            self.assertEqual(target.read_bytes(), b"model data")
            self.assertFalse(target.with_name("file.part").exists())
            self.assertEqual(comfy_setup.download(asset(), target, opener), "present")
            self.assertEqual(opener.open.call_count, 1)

    def test_resume_requires_exact_range(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            spec = asset()
            target.with_name("file.part").write_bytes(b"model")
            target.with_name("file.part.json").write_text(json.dumps({k: spec[k] for k in ("size", "sha256")}))
            opener = Mock()
            opener.open.return_value = response(b" data", 206, {"Content-Range": "bytes 5-9/10", "Content-Length": "5"})
            comfy_setup.download(spec, target, opener)
            self.assertEqual(opener.open.call_args.args[0].get_header("Range"), "bytes=5-")
            self.assertEqual(target.read_bytes(), b"model data")

    def test_ignored_range_restarts_without_duplicating_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            spec = asset()
            target.with_name("file.part").write_bytes(b"model")
            target.with_name("file.part.json").write_text(json.dumps({k: spec[k] for k in ("size", "sha256")}))
            opener = Mock()
            opener.open.return_value = response(b"model data")
            comfy_setup.download(spec, target, opener)
            self.assertEqual(target.read_bytes(), b"model data")

    def test_bad_checksum_never_publishes_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            opener = Mock()
            opener.open.return_value = response(b"wrong data")
            with self.assertRaisesRegex(ValueError, "Checksum failed"):
                comfy_setup.download(asset(), target, opener)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name("file.part").exists())

    def test_existing_different_model_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file"
            target.write_bytes(b"user's model")
            opener = Mock()
            with self.assertRaisesRegex(ValueError, "differs"):
                comfy_setup.download(asset(), target, opener)
            self.assertEqual(target.read_bytes(), b"user's model")
            opener.open.assert_not_called()

    def test_node_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, target = Path(directory) / "node.zip", Path(directory) / "node"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("repo/__init__.py", "")
                zipped.writestr("repo/../../escape.py", "bad")
            with self.assertRaises(ValueError):
                comfy_setup.extract_node(archive, target)
            self.assertFalse(target.exists())

    def test_pinned_node_archive_is_extracted_without_replacing_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, target = Path(directory) / "node.zip", Path(directory) / "node"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("repo/__init__.py", "NODE_CLASS_MAPPINGS={}")
            comfy_setup.extract_node(archive, target)
            self.assertTrue((target / "__init__.py").is_file())
            with self.assertRaises(FileExistsError):
                comfy_setup.extract_node(archive, target)

    def test_credentials_do_not_follow_cross_host_redirects(self):
        req = request.Request("https://huggingface.co/file", headers={"Authorization": "Bearer secret"})
        redirected = comfy_setup.SafeRedirect().redirect_request(req, None, 302, "", {}, "https://cdn.example/file")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_dependency_conflict_stops_before_pip_install(self):
        installed = [{"name": "torch", "version": "2.10.0+cu130"}, {"name": "torchaudio", "version": "2.10.0+cu130"}, {"name": "numpy", "version": "2.2.6"}]
        listed = Mock(stdout=json.dumps(installed), returncode=0)
        failed = Mock(stdout="conflict", stderr="", returncode=1)
        with patch.object(comfy_setup.subprocess, "run", side_effect=[listed, failed]) as run:
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                comfy_setup.install_requirements("comfy-python", ["test-package"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("--dry-run", run.call_args.args[0])

    def test_images_selection_never_downloads_video_or_moss(self):
        manifest = {"assets": [{**asset(), "group": "images", "destination": "loras/image.safetensors", "source": "https://example.org"},
                               {**asset(), "group": "video", "destination": "vae/video.safetensors", "source": "https://example.org"}],
                    "custom_nodes": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("")
            with patch.object(comfy_setup, "running_model_paths", return_value={}), patch.object(comfy_setup, "find_comfy_python", return_value=None), patch.object(comfy_setup, "download") as download, patch.object(comfy_setup, "check_nodes"):
                result = comfy_setup.install_groups(root, ["images"], manifest=manifest)
            self.assertEqual(result["failures"], [])
            self.assertEqual(download.call_count, 1)
            self.assertEqual(download.call_args.args[0]["group"], "images")

    def test_running_comfy_paths_include_extra_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = str(Path(directory) / "extra")
            reply = response(json.dumps({"diffusion_models": [folder], "bad": "not a list"}).encode())
            with patch.object(comfy_setup.request, "urlopen", return_value=reply) as fetch:
                paths = comfy_setup.running_model_paths("http://comfy/")
            self.assertEqual(paths, {"diffusion_models": [Path(folder)]})
            self.assertEqual(fetch.call_args.args[0], "http://comfy/internal/folder_paths")

    def test_extra_folder_nested_model_skips_download_and_hashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "comfy"
            root.mkdir()
            (root / "main.py").write_text("")
            extra = Path(directory) / "extra-unet"
            existing = extra / "nested/test.safetensors"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"x" * 100_050)
            spec = {**asset(b"y" * 100_000), "group": "images", "destination": "diffusion_models/test.safetensors"}
            manifest = {"assets": [spec], "custom_nodes": []}
            with patch.object(comfy_setup, "running_model_paths", return_value={"unet": [extra]}), patch.object(comfy_setup, "find_comfy_python", return_value=None), patch.object(comfy_setup, "download") as download, patch.object(comfy_setup, "sha256_file") as hashing, patch.object(comfy_setup, "check_nodes"):
                result = comfy_setup.install_groups(root, ["images"], manifest=manifest)
            self.assertEqual(result["failures"], [])
            download.assert_not_called()
            hashing.assert_not_called()
            self.assertFalse((root / "models").exists())

    def test_model_search_rejects_wrong_name_and_size_and_continues_to_other_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            spec = {**asset(b"x" * 100_000), "group": "images", "destination": "text_encoders/test.safetensors"}
            (first / "test.safetensors").write_bytes(b"x" * 90_000)
            (first / "different.safetensors").write_bytes(b"x" * 100_000)
            paths = {"text_encoders": [first], "clip": [second]}
            self.assertIsNone(comfy_setup.find_present_model(root / "models", spec, paths))
            match = second / "test.safetensors"
            match.write_bytes(b"x" * 100_000)
            self.assertEqual(comfy_setup.find_present_model(root / "models", spec, paths), match)

    def test_model_size_tolerance_is_bounded_and_rejects_empty_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model"
            with target.open("wb") as stream:
                stream.truncate(2_000_000_000 + 1024 * 1024)
            self.assertTrue(comfy_setup.model_size_matches(target, 2_000_000_000))
            with target.open("wb") as stream:
                stream.truncate(2_000_000_000 + 1024 * 1024 + 1)
            self.assertFalse(comfy_setup.model_size_matches(target, 2_000_000_000))
            target.write_bytes(b"")
            self.assertFalse(comfy_setup.model_size_matches(target, 100))

    def test_model_download_and_existing_model_do_not_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "test.safetensors"
            spec = {**asset(), "group": "images"}
            opener = Mock()
            opener.open.return_value = response(b"other data")
            with patch.object(comfy_setup, "sha256_file") as hashing:
                self.assertEqual(comfy_setup.download(spec, target, opener), "installed")
                self.assertEqual(comfy_setup.download(spec, target, opener), "present")
            hashing.assert_not_called()
            self.assertEqual(opener.open.call_count, 1)

    def test_repository_files_are_not_reused_from_unrelated_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong = root / "TTS/other/config.json"
            wrong.parent.mkdir(parents=True)
            wrong.write_bytes(b"model data")
            spec = {**asset(), "group": "video", "destination": "TTS/moss_tts/desired/config.json"}
            self.assertIsNone(comfy_setup.find_present_model(root, spec, {}))

    def test_missing_models_use_runtime_category_and_explicit_destination_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models, extra = root / "models", root / "extra"
            spec = {**asset(), "group": "images", "destination": "loras/test.safetensors"}
            paths = {"loras": [extra]}
            self.assertEqual(comfy_setup.model_download_target(models, spec, paths), extra / "test.safetensors")
            self.assertEqual(comfy_setup.model_download_target(models, spec, paths, True), models / "loras/test.safetensors")

    def test_noninteractive_setup_never_downloads_without_explicit_selection(self):
        with patch("sys.stdin.isatty", return_value=False), patch.object(comfy_setup, "describe_groups"), patch.object(comfy_setup, "install_groups") as install:
            self.assertIsNone(comfy_setup.offer_install("ComfyUI"))
            install.assert_not_called()

    def test_setup_saves_missing_helper_paths_without_overwriting_user_settings(self):
        import setup_project
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            original = "TELEGRAM_BOT_TOKEN=private-token\nCOMFY_PYTHON=\nFFMPEG_PATH=/my/ffmpeg\nCOMFYUI_PATH=/my/comfy\n"
            env.write_text(original)
            with patch.object(setup_project, "DOT_ENV", env):
                setup_project.generate_env_file("/detected/comfy", comfy_python="/detected/python", ffmpeg="/detected/ffmpeg")
            text = env.read_text()
            self.assertIn("TELEGRAM_BOT_TOKEN=private-token", text)
            self.assertIn("COMFY_PYTHON=/detected/python", text)
            self.assertIn("FFMPEG_PATH=/my/ffmpeg", text)
            self.assertIn("COMFYUI_PATH=/my/comfy", text)

    def test_segmentation_model_targets_project_and_respects_configured_path(self):
        spec = {"name": "u2net_human_seg.onnx", "storage": "project", "destination": "models/u2net_human_seg.onnx"}
        with patch.dict("os.environ", {"IDENTITY_SEGMENTATION_MODEL": ""}):
            self.assertEqual(comfy_setup.asset_target(Path("Comfy/models"), spec), comfy_setup.ROOT / "models/u2net_human_seg.onnx")
        with patch.dict("os.environ", {"IDENTITY_SEGMENTATION_MODEL": "custom/u2net.onnx"}):
            self.assertEqual(comfy_setup.asset_target(Path("Comfy/models"), spec), comfy_setup.ROOT / "custom/u2net.onnx")

    def test_video_helper_does_not_replace_existing_ffmpeg(self):
        with patch.object(comfy_setup.shutil, "which", return_value="/existing/ffmpeg"), patch.object(comfy_setup.subprocess, "run") as run:
            self.assertEqual(comfy_setup.resolve_ffmpeg("comfy-python"), "/existing/ffmpeg")
            run.assert_not_called()

    def test_setup_manifests_exclude_retired_civitai_models(self):
        manifest = comfy_setup.load_dependencies()
        retired = {"KNPV3_1.safetensors", "gonzalomoKrea2_v40.safetensors"}
        self.assertFalse(retired.intersection(a["name"] for a in manifest["assets"]))
        self.assertFalse(any("civitai.com" in a["url"] for a in manifest["assets"]))
        with patch("builtins.print") as output:
            comfy_setup.describe_groups(["images"], manifest)
        self.assertNotIn("civitai", str(output.call_args_list).lower())
        models = json.loads((comfy_setup.ROOT / "config/model_manifest.json").read_text())
        self.assertFalse(retired.intersection(m["name"] for m in models["models"]))


if __name__ == "__main__":
    unittest.main()
