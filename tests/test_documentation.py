"""Release documentation must point to shipped files and executable entry points."""
from pathlib import Path
import re
import unittest
from scripts import build_release

ROOT = Path(__file__).resolve().parents[1]
INTERNAL_DOCUMENTS = {ROOT / "docs/PRE_RELEASE_REVIEW.md", ROOT / "docs/GITHUB_UPLOAD.md"}
DOCUMENTS = sorted(set([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")]) - INTERNAL_DOCUMENTS)


class DocumentationTests(unittest.TestCase):
    def test_local_markdown_links_resolve_inside_source_tree(self):
        shipped = set(build_release.release_files(ROOT))
        for document in DOCUMENTS:
            for target in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", document.read_text(encoding="utf-8")):
                if re.match(r"[a-zA-Z]+:|#", target):
                    continue
                path = (document.parent / target.split("#", 1)[0]).resolve()
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(path.is_relative_to(ROOT))
                    self.assertTrue(path.is_file(), str(path))
                    self.assertIn(path, shipped, "Link points outside public release inventory")

    def test_documented_python_entry_points_are_shipped(self):
        for document in DOCUMENTS:
            for command in re.findall(r"\bpython(?:\.exe)?\s+([\w./-]+\.py)\b", document.read_text(encoding="utf-8")):
                with self.subTest(document=document.name, command=command):
                    self.assertTrue((ROOT / command).is_file(), command)

    def test_release_policy_includes_all_user_documentation(self):
        shipped = set(build_release.release_files(ROOT))
        for document in DOCUMENTS:
            with self.subTest(document=document.name):
                self.assertIn(document, shipped)


if __name__ == "__main__":
    unittest.main()
