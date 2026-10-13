"""Release packages exclude runtime/private state and preserve verified sources."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from scripts import build_release as builder


class ReleaseBuilderTests(unittest.TestCase):
    def source(self, root):
        for path in (*builder.ROOT_FILES,
                     *(f'docs/{p}' for p in builder.DOCS),
                     *(f'scripts/{p}' for p in builder.SCRIPTS),
                     *(f'config/{p}' for p in builder.CONFIG),
                     *(f'web/dist/{p}' for p in builder.WEB),
                     'vps/schema.sql', 'vps/requirements.txt', 'vps/app.py',
                     'worker/requirements.txt', 'worker/worker.py',
                     'worker/workflows/template.json', 'tests/test_example.py'):
            target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('TELEGRAM_BOT_TOKEN=\n' if path=='.env.example' else builder.VERSION + '\n' if path=='VERSION' else 'source\n',encoding='utf-8')
        return root

    def test_release_contains_requirements_and_sources_without_private_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.source(Path(folder)/'source')
            for name in ('.env','vps/jobs.db','vps/private.txt','vps/private.json',
                         'logs/worker.log','data/media/private.png','output/video.mp4',
                         'models/weights.onnx','.venv/private.py','worker/__pycache__/stale.pyc',
                         'config/private.json','web/dist/private.json','scripts/private.py',
                         'docs/WEB_FRONTEND_PLAN.md'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private')
            target,archive,manifest=builder.build_release(root,Path(folder)/'release')
            names={row['path'] for row in manifest['files']}
            self.assertIn('vps/requirements.txt',names)
            self.assertIn('worker/requirements.txt',names)
            self.assertIn('web/dist/brand.css',names)
            self.assertIn('worker/workflows/template.json',names)
            self.assertIn('tests/test_example.py',names)
            self.assertNotIn('.env',names)
            self.assertFalse(any('private' in name or '__pycache__' in name for name in names))
            for row in manifest['files']:
                self.assertEqual(hashlib.sha256((target/row['path']).read_bytes()).hexdigest(),row['sha256'])
            with zipfile.ZipFile(archive) as package:
                self.assertIsNone(package.testzip())
                self.assertEqual(set(package.namelist()),{f'{builder.DEFAULT_NAME}/{p}' for p in names|{'release_manifest.json'}})
            self.assertEqual(json.loads((target/'release_manifest.json').read_text()),manifest)

    def test_internal_release_documents_are_excluded_and_local_copies_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.source(Path(folder) / 'source')
            internal = ('docs/PRE_RELEASE_REVIEW.md', 'docs/GITHUB_UPLOAD.md')
            for name in internal:
                (root / name).write_text('internal release notes', encoding='utf-8')
            target, archive, manifest = builder.build_release(root, Path(folder) / 'release')
            names = {row['path'] for row in manifest['files']}
            with zipfile.ZipFile(archive) as package:
                for name in internal:
                    self.assertNotIn(name, names)
                    self.assertNotIn(builder.DEFAULT_NAME + '/' + name, package.namelist())
                    self.assertFalse((target / name).exists())
                    self.assertEqual((root / name).read_text(), 'internal release notes')
            self.assertIn('docs/MODELS.md', names)
            self.assertIn('docs/GETTING_STARTED.md', names)
            self.assertIn('docs/RELEASE_CHECKLIST.md', names)

    def test_public_beta_version_and_archive_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.source(Path(folder) / 'source')
            target, archive, manifest = builder.build_release(root, Path(folder) / 'release')
            self.assertEqual(target.name, 'CastWorkshop-v0.9.0-beta.1')
            self.assertEqual(archive.name, 'CastWorkshop-v0.9.0-beta.1.zip')
            self.assertEqual(manifest['version'], '0.9.0-beta.1')
            self.assertEqual(manifest['release_label'], 'CastWorkshop 0.9 Public Beta')
            self.assertEqual((target / 'VERSION').read_text().strip(), manifest['version'])

    def test_explicit_legacy_rc_packaging_names_remain_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.source(Path(folder) / 'source')
            target, archive, _ = builder.build_release(root, Path(folder) / 'release', 'CastWorkshop_RC5')
            self.assertEqual(target.name, 'CastWorkshop_RC5')
            self.assertEqual(archive.name, 'CastWorkshop_RC5.zip')

    def test_equal_sources_produce_identical_archives(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.source(Path(folder)/'source')
            _,first,_=builder.build_release(root,Path(folder)/'one')
            _,second,_=builder.build_release(root,Path(folder)/'two')
            self.assertEqual(first.read_bytes(),second.read_bytes())

    def test_existing_release_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.source(Path(folder)/'source');output=Path(folder)/'release'
            target,archive,_=builder.build_release(root,output)
            before=archive.read_bytes()
            with self.assertRaises(FileExistsError):builder.build_release(root,output)
            self.assertEqual(archive.read_bytes(),before)
            self.assertTrue(target.is_dir())

    def test_nonempty_example_credentials_are_rejected_before_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.source(Path(folder)/'source');output=Path(folder)/'release'
            (root/'.env.example').write_text('TELEGRAM_BOT_TOKEN=private\n')
            with self.assertRaisesRegex(ValueError,'nonempty credential'):builder.build_release(root,output)
            self.assertFalse(output.exists())

    def test_legacy_or_traversal_release_names_are_rejected(self):
        for name in ('CharacterCreator_RC5','../CastWorkshop_RC5','CastWorkshop_RC0',
                     '../CastWorkshop-v0.9.0-beta.1', 'CastWorkshop-v0.9.0-beta.0',
                     'CastWorkshop-v00.9.0', 'CastWorkshop-v0.9.0-beta.1/extra'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder.build_release('.', '.',name)

    def test_linked_source_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.source(Path(folder)/'source')
            original=root/'README.md';original.unlink()
            outside=Path(folder)/'private.md';outside.write_text('private')
            try:original.symlink_to(outside)
            except OSError:self.skipTest('This host does not permit creating test symlinks')
            with self.assertRaisesRegex(ValueError,'linked path'):
                builder.build_release(root,Path(folder)/'release')
