import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import comfy_setup, runtime


class SetupResilienceTests(unittest.TestCase):
    def test_atomic_json_retries_windows_lock_without_exposing_partial_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")
            replace = runtime.os.replace
            calls = []
            def locked_then_replace(source, target):
                calls.append(1)
                if len(calls) < 3:
                    self.assertEqual(json.loads(path.read_text()), {"old": True})
                    failure = PermissionError("temporarily locked")
                    failure.winerror = 5
                    raise failure
                replace(source, target)
            with patch.object(runtime.os, "replace", side_effect=locked_then_replace), patch.object(runtime.time, "sleep") as sleep:
                runtime.atomic_json(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_persistent_windows_lock_is_bounded_and_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")
            failure = PermissionError("persistent lock")
            failure.winerror = 32
            with patch.object(runtime.os, "replace", side_effect=failure) as replace, patch.object(runtime.time, "sleep"):
                with self.assertRaises(PermissionError):
                    runtime.atomic_json(path, {"new": True})
            self.assertEqual(replace.call_count, 9)
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_normal_permission_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(runtime.os, "replace", side_effect=PermissionError("permissions")) as replace, patch.object(runtime.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    runtime.atomic_json(Path(folder) / "state.json", {})
            replace.assert_called_once()
            sleep.assert_not_called()

    def test_project_ca_bundle_supplements_system_trust_without_disabling_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / ".venv/Lib/site-packages/certifi/cacert.pem"
            bundle.parent.mkdir(parents=True)
            bundle.touch()
            context = Mock()
            with patch.object(comfy_setup, "ROOT", root), patch.object(comfy_setup.ssl, "create_default_context", return_value=context) as create:
                self.assertIs(comfy_setup.download_context(), context)
            create.assert_called_once_with()
            context.load_verify_locations.assert_called_once_with(cafile=str(bundle))
            self.assertNotIn("verify_mode", context.__dict__)
            self.assertNotIn("check_hostname", context.__dict__)


if __name__ == "__main__":
    unittest.main()
