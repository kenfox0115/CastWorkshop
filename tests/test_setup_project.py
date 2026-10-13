import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import setup_project


class SetupProjectTests(unittest.TestCase):
    def test_voice_tools_are_prepared_in_application_python(self):
        with patch.object(setup_project.subprocess, "run") as run:
            self.assertTrue(setup_project.prepare_voice_trimming("app-python"))
        self.assertEqual(run.call_args.args[0], ["app-python", "-m", "worker.voice_trim", "--prepare"])
        self.assertEqual(run.call_args.kwargs["cwd"], setup_project.PROJECT_ROOT)

    def test_failed_voice_preparation_reports_not_ready(self):
        with patch.object(setup_project.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "prepare")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(setup_project.prepare_voice_trimming("app-python"))
        self.assertIn("Voice trimming is not ready", output.getvalue())

    def test_root_setup_import_from_another_working_directory(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                 "import setup_project as s; assert s.PROJECT_ROOT == s.DOT_ENV.parent; "
                 "assert s.REQ_VPS.is_file(); assert s.SCHEMA_SQL.is_file(); "
                 "assert (s.PROJECT_ROOT / 'start.py').is_file()", str(root)],
                cwd=folder, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(setup_project.PROJECT_ROOT, root)

    def test_headless_fresh_configuration_never_prompts_for_optional_secret(self):
        for token in ("", "123456:example-token"):
            with self.subTest(configured=bool(token)), tempfile.TemporaryDirectory() as folder:
                env = Path(folder) / ".env"
                with (patch.object(setup_project, "DOT_ENV", env),
                      patch("sys.stdin.isatty", return_value=False),
                      patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token}, clear=True),
                      patch("getpass.getpass") as prompt,
                      patch("builtins.input") as network_prompt):
                    setup_project.generate_env_file()
                prompt.assert_not_called()
                network_prompt.assert_not_called()
                text = env.read_text(encoding="utf-8")
                self.assertIn(f"TELEGRAM_BOT_TOKEN={token}\n", text)
                self.assertIn(f"TELEGRAM_ENABLED={int(bool(token))}\n", text)
                self.assertIn("API_HOST=127.0.0.1\n", text)

    def test_fresh_interactive_network_default_and_explicit_local_choice(self):
        for answer in ("", "1"):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as folder:
                env = Path(folder) / ".env"
                with (patch.object(setup_project, "DOT_ENV", env),
                      patch.dict(os.environ, {}, clear=True),
                      patch("sys.stdin.isatty", return_value=True),
                      patch("builtins.input", return_value=answer) as choice,
                      patch("getpass.getpass", return_value=""),
                      contextlib.redirect_stdout(io.StringIO())):
                    setup_project.generate_env_file()
                choice.assert_called_once()
                self.assertIn("API_HOST=127.0.0.1\n", env.read_text())

    def test_fresh_interactive_lan_choice_warns_before_saving(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            output = io.StringIO()
            with (patch.object(setup_project, "DOT_ENV", env),
                  patch.dict(os.environ, {}, clear=True),
                  patch("sys.stdin.isatty", return_value=True),
                  patch("builtins.input", return_value="2"),
                  patch("getpass.getpass", return_value=""),
                  contextlib.redirect_stdout(output)):
                setup_project.generate_env_file()
                self.assertEqual(os.environ["API_HOST"], "0.0.0.0")
            self.assertIn("API_HOST=0.0.0.0\n", env.read_text())
            text = output.getvalue()
            self.assertIn("no built-in login or HTTPS", text)
            self.assertLess(text.index("no built-in login or HTTPS"), text.index(".env created"))

    def test_headless_explicit_environment_host_is_saved_without_prompt(self):
        for host in ("127.0.0.1", "0.0.0.0"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as folder:
                env = Path(folder) / ".env"
                with (patch.object(setup_project, "DOT_ENV", env),
                      patch.dict(os.environ, {"API_HOST": host}, clear=True),
                      patch("sys.stdin.isatty", return_value=False),
                      patch("builtins.input") as prompt,
                      contextlib.redirect_stdout(io.StringIO())):
                    setup_project.generate_env_file()
                prompt.assert_not_called()
                self.assertIn(f"API_HOST={host}\n", env.read_text())

    def test_existing_host_configuration_is_preserved_in_both_setup_modes(self):
        for host in ("127.0.0.1", "0.0.0.0"):
            for interactive in (False, True):
                with self.subTest(host=host, interactive=interactive), tempfile.TemporaryDirectory() as folder:
                    env = Path(folder) / ".env"
                    original = f"API_HOST={host}\nTELEGRAM_BOT_TOKEN=\n"
                    env.write_text(original)
                    with (patch.object(setup_project, "DOT_ENV", env),
                          patch.dict(os.environ, {}, clear=True),
                          patch("sys.stdin.isatty", return_value=interactive),
                          patch("builtins.input") as prompt,
                          contextlib.redirect_stdout(io.StringIO())):
                        setup_project.generate_env_file()
                    prompt.assert_not_called()
                    self.assertEqual(env.read_text(), original)

    def test_existing_configuration_without_network_choice_gets_safe_headless_default(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("TELEGRAM_BOT_TOKEN=\n")
            with (patch.object(setup_project, "DOT_ENV", env),
                  patch.dict(os.environ, {}, clear=True),
                  patch("sys.stdin.isatty", return_value=False),
                  patch("builtins.input") as prompt,
                  contextlib.redirect_stdout(io.StringIO())):
                setup_project.generate_env_file()
            prompt.assert_not_called()
            self.assertIn("API_HOST=127.0.0.1\n", env.read_text())

    def test_start_requires_interactive_yes(self):
        for interactive, answer in ((False, "yes"), (True, ""), (True, "n")):
            with (
                self.subTest(interactive=interactive, answer=answer),
                patch("sys.stdin.isatty", return_value=interactive),
                patch("builtins.input", return_value=answer) as prompt,
                patch.object(setup_project.subprocess, "run") as run,
            ):
                self.assertEqual(setup_project.offer_start(), 0)
                run.assert_not_called()
                if not interactive:
                    prompt.assert_not_called()

    def test_start_uses_launcher_and_propagates_failure(self):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value=" YES "),
            patch.object(setup_project.subprocess, "run", return_value=Mock(returncode=1)) as run,
        ):
            self.assertEqual(setup_project.offer_start(), 1)
        run.assert_called_once_with(
            [setup_project.sys.executable, str(setup_project.PROJECT_ROOT / "start.py")],
            cwd=setup_project.PROJECT_ROOT, check=False)

    def test_default_model_installs_public_source_then_creates_local_alias(self):
        report = {
            "models": [
                {"engine": "ollama", "name": "qwen3-14b-64k", "status": "missing"},
            ],
        }
        completed = Mock(returncode=0)
        with (
            patch.dict(os.environ, {"OLLAMA_MODEL": "qwen3-14b-64k"}, clear=False),
            patch.object(setup_project, "readiness", return_value=report),
            patch.object(setup_project, "ollama_is_installed", return_value=True),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
            patch("subprocess.run", return_value=completed) as run,
        ):
            setup_project.check_and_offer_models(False)

        source = "huihui_ai/qwen3-abliterated:14b-v2-q8_0"
        self.assertEqual(run.call_args_list[0].args[0], ["ollama", "pull", source])
        create = run.call_args_list[1].args[0]
        self.assertEqual(create[:4], ["ollama", "create", "qwen3-14b-64k", "-f"])
        self.assertTrue(create[4].endswith("qwen3-14b-64k.Modelfile"))

    def test_custom_model_still_pulls_its_own_registry_tag(self):
        report = {
            "models": [
                {"engine": "ollama", "name": "custom/model:tag", "status": "missing"},
            ],
        }
        with (
            patch.dict(os.environ, {"OLLAMA_MODEL": "custom/model:tag"}, clear=False),
            patch.object(setup_project, "readiness", return_value=report),
            patch.object(setup_project, "ollama_is_installed", return_value=True),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
            patch("subprocess.run", return_value=Mock(returncode=0)) as run,
        ):
            setup_project.check_and_offer_models(False)

        run.assert_called_once_with(["ollama", "pull", "custom/model:tag"], check=False)


if __name__ == "__main__":
    unittest.main()
