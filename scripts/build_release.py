"""Build a source-only CastWorkshop release from an explicit, reviewed file policy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
DEFAULT_NAME = f"CastWorkshop-v{VERSION}"
RELEASE_LABEL = "CastWorkshop 0.9 Public Beta"
ROOT_FILES = ('.env.example', '.gitattributes', '.gitignore', 'AGENTS.md',
              'CONTRIBUTING.md', 'CHANGELOG.md', 'VERSION', '.github/PULL_REQUEST_TEMPLATE.md', 'LICENSE', 'README.md', 'SECURITY.md',
              'start.py', 'stop.py', 'restart.py', 'setup_project.py')
DOCS = ('GETTING_STARTED.md', 'IMAGE_DEFAULTS.md', 'IMAGE_PROMPTS.md',
        'MODELS.md', 'RELEASE_CHECKLIST.md', 'PERFORMANCE_PLANNER.md', 'VOICE_SELECTION.md')
SCRIPTS = ('__init__.py', 'build_release.py', 'clear_stuck_jobs.py', 'comfy_setup.py',
           'inspect_env.py', 'isolate_identity.py', 'model_manifest.py',
           'recovery_report.py', 'run_checks.py', 'runtime.py', 'setup_logging.py',
           'supervisor.py', 'verify.py', 'voice_reference.py',
           'experiment_h3_moss_dialogue.py')
CONFIG = ('model_manifest.json', 'setup_dependencies.json',
          'ollama/qwen3-14b-64k.Modelfile')
WEB = ('app.css', 'app.js', 'castworkshop-icon.png', 'castworkshop-mark.png',
       'brand.css', 'controls.css', 'controls.js', 'favicon.svg', 'index.html')


def release_files(source):
    """No ignored directory, credential file, junction, or symlink is copied."""
    source = Path(source).resolve()
    selected = [source / name for name in ROOT_FILES]
    selected += [source / 'docs' / name for name in DOCS]
    selected += [source / 'scripts' / name for name in SCRIPTS]
    selected += [source / 'config' / name for name in CONFIG]
    selected += [source / 'web/dist' / name for name in WEB]
    selected += [source / 'vps/schema.sql', source / 'vps/requirements.txt',
                 source / 'worker/requirements.txt']
    for folder, extensions in (('vps', {'.py'}),
                               ('worker', {'.py', '.json'}),
                               ('tests', {'.py', '.json'})):
        for directory, dirs, files in os.walk(source / folder, followlinks=False):
            base = Path(directory)
            for name in list(dirs):
                path = base / name
                if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                    raise ValueError(f'Release source contains a linked directory: {path.relative_to(source)}')
            dirs[:] = [name for name in dirs if name != '__pycache__']
            selected += [base / name for name in files if Path(name).suffix in extensions]
    for path in selected:
        # Check every ancestor: fixed entries can also traverse linked folders.
        relative = path.relative_to(source)
        for parent in (path, *path.parents):
            if parent == source:
                break
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise ValueError(f'Release source contains a linked path: {relative}')
        if not path.is_file() or not path.resolve().is_relative_to(source):
            raise ValueError(f'Missing or unsafe release input: {relative}')
        if path.name.startswith('.env') and relative.as_posix() != '.env.example':
            raise ValueError(f'Credential file in release input: {relative}')
    return sorted(set(selected), key=lambda path: path.relative_to(source).as_posix())


def build_release(source, output, name=DEFAULT_NAME):
    public_name = r'CastWorkshop-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:alpha|beta|rc)\.[1-9][0-9]*)?'
    if not re.fullmatch(public_name, name) and not re.fullmatch(r'CastWorkshop_RC[1-9][0-9]*', name):
        raise ValueError('Release name must use CastWorkshop-vX.Y.Z[-beta.N] or a legacy CastWorkshop_RC number')
    source = Path(source).resolve()
    output = Path(output).resolve()
    target, archive = output / name, output / f'{name}.zip'
    if target == source or source.is_relative_to(target):
        raise ValueError('Release destination must not contain the source tree')
    if target.exists() or archive.exists():
        raise FileExistsError('Release folder or ZIP already exists; choose a new release name')
    files = release_files(source)
    # The only environment file shipped is a template with empty credentials.
    for line in (source / '.env.example').read_text(encoding='utf-8-sig').splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and any(word in key.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')) and value.strip().strip('\"\''):
            raise ValueError('Environment example contains a nonempty credential')
    output.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    entries = []
    for path in files:
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        data = destination.read_bytes()
        entries.append({'path': relative.as_posix(), 'bytes': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'release': name, 'version': VERSION, 'release_label': RELEASE_LABEL, 'format': 'source', 'files': entries,
                'excluded': ['.git', '.env', 'databases', 'runtime logs', 'generated media',
                             'virtual environments', 'model weights', 'developer planning notes',
                             'job-specific live diagnostics']}
    (target / 'release_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    # Stable paths, timestamps and permissions make equal source packages reproducible.
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as package:
        for path in sorted(target.rglob('*')):
            if path.is_file():
                info = zipfile.ZipInfo(f'{name}/{path.relative_to(target).as_posix()}')
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                package.writestr(info, path.read_bytes())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / f'{name}.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    return target, archive, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'releases')
    parser.add_argument('--name', default=DEFAULT_NAME)
    args = parser.parse_args()
    target, archive, manifest = build_release(ROOT, args.output, args.name)
    print(f'Release folder: {target}')
    print(f'Release archive: {archive}')
    print(f'Source files: {len(manifest["files"])}; manifest and archive checksum generated')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
