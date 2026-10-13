"""Optional, portable installation of pinned ComfyUI workflow dependencies.

No ComfyUI processes are started/stopped and no third-party installer is executed.
Only explicitly selected groups are installed; existing node code is preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from urllib import error, parse, request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "setup_dependencies.json"
GROUPS = ("images", "video")
PROTECTED = {"torch", "torchvision", "torchaudio", "numpy", "pillow", "triton", "triton-windows"}


def load_dependencies(path=MANIFEST):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("Unsupported setup dependency manifest")
    for asset in data["assets"] + data["custom_nodes"]:
        if not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]):
            raise ValueError("Every download must have a pinned SHA256")
        if parse.urlsplit(asset["url"]).scheme != "https":
            raise ValueError("Installer sources must use HTTPS")
        if not isinstance(asset["size"], int) or asset["size"] <= 0:
            raise ValueError("Invalid download size")
    for asset in data["assets"]:
        safe_relative(asset["destination"])
        if asset.get("storage", "comfyui") not in ("project", "comfyui"):
            raise ValueError("Unknown model storage location")
        if asset["group"] not in GROUPS:
            raise ValueError("Unknown installation group")
    for node in data["custom_nodes"]:
        if not re.fullmatch(r"[a-f0-9]{40}", node["revision"]):
            raise ValueError("Node revision must be a pinned commit")
        if safe_relative(node["name"]).parts != (node["name"],):
            raise ValueError("Invalid node directory")
    return data


def safe_relative(value):
    if "\\" in value or ":" in value:
        raise ValueError("Unsafe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ("..", ".") for p in path.parts):
        raise ValueError("Unsafe relative path")
    return path


def destination(root, relative):
    root = Path(root).resolve()
    path = root.joinpath(*safe_relative(relative).parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Destination escapes the selected directory")
    return path


def sha256_file(path):
    digest = hashlib.sha256()
    last = time.monotonic()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            if time.monotonic() - last >= 5:
                print(f"[setup] Verifying {Path(path).name}...", flush=True)
                last = time.monotonic()
    return digest.hexdigest()


def model_target(models, relative):
    """Respect a user-configured/junction-backed category directory."""
    parts = safe_relative(relative).parts
    if len(parts) < 2:
        raise ValueError("Models need a category directory")
    return destination((Path(models) / parts[0]).resolve(), "/".join(parts[1:]))


def model_size_matches(path, size):
    """Trust model bytes by size alone, with a bounded 0.1% tolerance."""
    tolerance = min(1024 * 1024, int(size * 0.001))
    try:
        actual = path.stat().st_size
        return path.is_file() and actual > 0 and abs(actual - size) <= tolerance
    except OSError:
        return False


def running_model_paths(comfy_url):
    """Ask the active server for its effective paths, including extra model paths."""
    try:
        with request.urlopen(comfy_url.rstrip("/") + "/internal/folder_paths", timeout=10) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("Invalid folder inventory")
        return {category: [Path(value).expanduser() for value in paths
                           if isinstance(value, str) and Path(value).is_absolute()]
                for category, paths in data.items() if isinstance(paths, list)}
    except (OSError, ValueError):
        print("[setup][warn] Running ComfyUI model folders unavailable; checking configured local folders only")
        return {}


def model_locations(models, asset, runtime_paths):
    """Return category roots and relative filename without mixing repository files."""
    parts = safe_relative(asset["destination"]).parts
    category = parts[0]
    aliases = {"diffusion_models": ("diffusion_models", "unet"),
               "text_encoders": ("text_encoders", "clip"),
               "TTS": ("TTS", "tts", "moss-tts")}.get(category, (category,))
    roots = []
    for alias in aliases:
        roots.extend(runtime_paths.get(alias, []))
    roots.extend(Path(models) / alias for alias in aliases)
    return list(dict.fromkeys(roots)), Path(*parts[1:])


def find_present_model(models, asset, runtime_paths):
    if asset.get("storage") == "project":
        target = asset_target(models, asset)
        return target if model_size_matches(target, asset["size"]) else None
    roots, relative = model_locations(models, asset, runtime_paths)
    for root in roots:
        # Repository support files must stay in their intended repository tree.
        try:
            if len(relative.parts) == 1:
                candidates = list(root.rglob(relative.name))
            else:
                candidates = list((root / relative.parent).glob(relative.name))
        except OSError:
            print(f"[setup][warn] Could not inspect model folder: {root}")
            continue
        for path in candidates:
            if path.name == relative.name and model_size_matches(path, asset["size"]):
                return path
    return None


def model_download_target(models, asset, runtime_paths, explicit_models=False):
    if asset.get("storage") == "project" or explicit_models:
        return asset_target(models, asset)
    roots, relative = model_locations(models, asset, runtime_paths)
    return destination(roots[0].resolve(), relative.as_posix())


class SafeRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and parse.urlsplit(req.full_url).netloc != parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def network_error(exc, host):
    """Report a useful cause without exposing request URLs or credentials."""
    reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        cause = "TLS certificate verification failed; check the machine's trusted certificates and HTTPS proxy settings (verification remains enabled)"
    elif isinstance(reason, socket.gaierror):
        cause = "DNS lookup failed; check network/DNS access from this machine"
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        cause = "connection timed out; check network/firewall/proxy access"
    elif isinstance(reason, ConnectionRefusedError):
        cause = "connection refused; check network/firewall/proxy access"
    elif isinstance(reason, ssl.SSLError):
        cause = "TLS handshake failed; check HTTPS connectivity and proxy settings"
    else:
        cause = f"connection failed ({type(reason).__name__}, error code {getattr(reason, 'errno', None)})"
    return f"Download host {host}: {cause}"


def download_context():
    """Keep system trust and supplement it with the project's Mozilla CA bundle."""
    context = ssl.create_default_context()
    bundles = [ROOT / ".venv/Lib/site-packages/certifi/cacert.pem"]
    bundles.extend((ROOT / ".venv/lib").glob("python*/site-packages/certifi/cacert.pem"))
    try:
        import certifi
        bundles.append(Path(certifi.where()))
    except ImportError:
        pass
    for bundle in bundles:
        if bundle.is_file():
            context.load_verify_locations(cafile=str(bundle))
            break
    return context


def download(asset, target, opener=None):
    """Resume and publish atomically; hash node archives, trust model sizes."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    expected, size = asset["sha256"], asset["size"]
    is_model = asset.get("group") in GROUPS
    if target.exists():
        if (model_size_matches(target, size) if is_model else
                target.stat().st_size == size and sha256_file(target) == expected):
            print(f"[setup] Already installed: {target.name}")
            return "present"
        raise ValueError(f"Existing {target.name} differs from the pinned file; move it aside manually to replace it")
    partial = target.with_name(target.name + ".part")
    metadata = target.with_name(target.name + ".part.json")
    if partial.is_symlink() or metadata.is_symlink():
        raise ValueError("Installer partial files must not be symlinks")
    identity = {"sha256": expected, "size": size}
    if partial.exists():
        saved = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else None
        if saved != identity or partial.stat().st_size > size:
            raise ValueError(f"Unrecognized partial download for {target.name}; move its .part files aside")
    metadata.write_text(json.dumps(identity), encoding="utf-8")
    offset = partial.stat().st_size if partial.exists() else 0
    if shutil.disk_usage(target.parent).free < size - offset + 16 * 1024 * 1024:
        raise OSError(f"Insufficient free disk space for {target.name}")
    if offset != size:
        headers = {"User-Agent": "CastWorkshop-Setup", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        token_env = asset.get("auth_env")
        host = parse.urlsplit(asset["url"]).hostname
        if host == "huggingface.co":
            token_env = "HF_TOKEN"
        if token_env and os.environ.get(token_env):
            headers["Authorization"] = "Bearer " + os.environ[token_env]
        opener = opener or request.build_opener(SafeRedirect(), request.HTTPSHandler(context=download_context()))
        try:
            response = opener.open(request.Request(asset["url"], headers=headers), timeout=60)
        except error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(f"Access denied for {asset['name']}; review its publisher page and set {token_env or 'the required credentials'}") from None
            raise RuntimeError(f"Download for {asset['name']} returned HTTP {exc.code}") from None
        except error.URLError as exc:
            raise RuntimeError(network_error(exc, host)) from None
        with response:
            if response.status == 206:
                if response.headers.get("Content-Range", "") != f"bytes {offset}-{size - 1}/{size}":
                    raise ValueError("Server returned an inconsistent resume range")
            elif response.status == 200:
                offset = 0  # Range ignored: safely restart, never append duplicate bytes.
            else:
                raise ValueError(f"Unexpected download status {response.status}")
            if response.headers.get("Content-Length") and int(response.headers["Content-Length"]) != size - offset:
                raise ValueError(f"Unexpected file size for {asset['name']}")
            mode = "ab" if offset else "wb"
            print(f"[setup] Downloading {target.name} ({size / 1e9:.2f} GB), starting at {offset / size:.0%}", flush=True)
            last, downloaded = time.monotonic(), offset
            with partial.open(mode) as stream:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ValueError("Download exceeded its pinned size")
                    stream.write(chunk)
                    if time.monotonic() - last >= 2:
                        print(f"[setup] {target.name}: {downloaded / size:.0%}", flush=True)
                        last = time.monotonic()
    if partial.stat().st_size != size:
        raise ValueError(f"Incomplete {target.name}; rerun setup to resume")
    if not is_model and sha256_file(partial) != expected:
        partial.unlink()  # Known installer partial, never a user model.
        metadata.unlink(missing_ok=True)
        raise ValueError(f"Checksum failed for {target.name}; rerun to download again")
    partial.replace(target)
    metadata.unlink(missing_ok=True)
    print(f"[setup] {'Downloaded' if is_model else 'Verified'}: {target.name}")
    return "installed"


def extract_node(archive, target):
    """Extract one GitHub root without traversal or symlinks, then publish its directory."""
    target = Path(target)
    if target.exists():
        raise FileExistsError("Existing custom node will not be replaced")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".castworkshop-node-", dir=target.parent) as temp:
        staging = Path(temp) / "package"
        staging.mkdir()
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            roots = {safe_relative(m.filename).parts[0] for m in members}
            if len(roots) != 1:
                raise ValueError("Node archive must contain exactly one root")
            if sum(m.file_size for m in members) > 1024 * 1024 * 1024:
                raise ValueError("Node archive is unexpectedly large")
            for member in members:
                parts = safe_relative(member.filename).parts[1:]
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Node archive contains a symlink")
                if not parts:
                    continue
                path = destination(staging, "/".join(parts))
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as stream, path.open("wb") as output:
                        shutil.copyfileobj(stream, output)
        if not (staging / "__init__.py").is_file():
            raise ValueError("Node package is missing __init__.py")
        staging.rename(target)


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def find_comfy_python(comfy_path, explicit=None):
    root = Path(comfy_path)
    configured = explicit or os.environ.get("COMFY_PYTHON")
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        candidates = [
            root / ".venv" / "Scripts/python.exe", root / ".venv/bin/python",
            root.parent / ".venv/Scripts/python.exe", root.parent / ".venv/bin/python",
            root.parent / "python_embeded/python.exe", root.parent.parent / "python_embeded/python.exe",
            root.parent / "standalone-env/python.exe",
            root.parent / "standalone-env/bin/python",
        ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                probe = subprocess.run([str(candidate), "-c", "import sys, torch; print(sys.executable)"],
                                       env=clean_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return candidate.resolve()
    return None


def install_requirements(python, requirements):
    if not requirements:
        return
    listed = subprocess.run([str(python), "-m", "pip", "list", "--format=json"],
                            env=clean_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=60)
    versions = {item["name"].lower().replace("_", "-"): item["version"] for item in json.loads(listed.stdout)}
    protected = {name: version for name, version in versions.items()
                 if name in PROTECTED or name.startswith("comfy-") or name.startswith("comfyui")}
    if "torch" not in protected or "torchaudio" not in protected:
        raise RuntimeError("ComfyUI must already have its matching Torch and torchaudio installed")
    with tempfile.TemporaryDirectory(prefix="castworkshop-pip-") as directory:
        constraints, report = Path(directory) / "constraints.txt", Path(directory) / "plan.json"
        constraints.write_text("\n".join(f"{k}=={v}" for k, v in protected.items()), encoding="utf-8")
        command = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-c", str(constraints), *requirements]
        plan = subprocess.run([*command, "--dry-run", "--report", str(report)], env=clean_env(),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if plan.returncode:
            print(plan.stdout[-3000:])
            print(plan.stderr[-3000:])
            raise RuntimeError("Node dependencies conflict with ComfyUI's installed core packages; no packages were installed")
        changes = json.loads(report.read_text(encoding="utf-8"))["install"]
        for item in changes:
            name = item["metadata"]["name"].lower().replace("_", "-")
            if name in PROTECTED or name.startswith("comfy-") or name.startswith("comfyui"):
                raise RuntimeError(f"Dependency plan would modify core package {name}; installation stopped")
        if not changes:
            print("[setup] Node Python dependencies already satisfied")
            return
        print("[setup] Installing node dependencies: " + ", ".join(item["metadata"]["name"] for item in changes), flush=True)
        subprocess.run(command, env=clean_env(), check=True)


def required_classes(groups):
    classes = set()
    for path in (ROOT / "worker/workflows").rglob("*.json"):
        video = "minimax" in path.name.lower() or "MOSS" in path.name
        if ("video" if video else "images") in groups:
            classes.update(node["class_type"] for node in json.loads(path.read_text(encoding="utf-8")).values())
    return classes


def check_nodes(groups, comfy_url):
    try:
        with request.urlopen(comfy_url.rstrip("/") + "/object_info", timeout=15) as response:
            available = json.load(response)
    except (OSError, ValueError):
        print("[setup] Node registration unverified: start/restart ComfyUI, then rerun with --check-only")
        return False
    missing = sorted(required_classes(groups) - available.keys())
    if missing:
        print("[setup][warn] Nodes not registered: " + ", ".join(missing))
        print("[setup] Restart ComfyUI after node installation. Built-in nodes such as MiniMaxH3ReferenceToVideo require a recent ComfyUI release.")
        return False
    print("[setup] All required workflow node types are registered")
    return True


def resolve_ffmpeg(python):
    configured = os.environ.get("FFMPEG_PATH") or "ffmpeg"
    found = shutil.which(configured)
    if found:
        return found
    if not python:
        return ""
    try:
        probe = subprocess.run([str(python), "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
                               env=clean_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if probe.returncode == 0:
        path = Path(probe.stdout.strip())
        if path.is_file():
            return str(path)
    return ""


def asset_target(models, asset):
    if asset.get("storage") == "project":
        if asset["name"] == "u2net_human_seg.onnx" and os.environ.get("IDENTITY_SEGMENTATION_MODEL"):
            configured = Path(os.environ["IDENTITY_SEGMENTATION_MODEL"]).expanduser()
            return configured if configured.is_absolute() else ROOT / configured
        return destination(ROOT, asset["destination"])
    return model_target(models, asset["destination"])


def install_groups(comfy_path, groups, python=None, models_dir=None, comfy_url=None, manifest=None):
    groups = set(groups)
    if not groups or groups - set(GROUPS):
        raise ValueError("Choose images and/or video")
    root = Path(comfy_path).expanduser().resolve()
    if not (root / "main.py").is_file():
        raise ValueError("Choose the ComfyUI directory containing main.py")
    manifest = manifest or load_dependencies()
    models_dir = models_dir or os.environ.get("COMFY_MODELS_DIR")
    models = Path(models_dir).expanduser().resolve() if models_dir else root / "models"
    comfy_url = comfy_url or os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
    runtime_paths = running_model_paths(comfy_url)
    print(f"[setup] Local model fallback: {models}")
    failures, installed_nodes = [], False
    python = find_comfy_python(root, python)
    if python:
        print(f"[setup] ComfyUI Python: {python}")
    else:
        print("[setup][warn] ComfyUI Python not found. Set COMFY_PYTHON or use --comfy-python; model downloads can still proceed.")
    requirements = sorted({req for group in groups for req in manifest.get("python_dependencies", {}).get(group, [])})
    if requirements:
        try:
            if not python:
                raise RuntimeError("ComfyUI Python is required for the identity-isolation/video helper dependencies")
            install_requirements(python, requirements)
        except Exception as exc:
            print(f"[setup][warn] Helper Python dependencies: {str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}")
            failures.append("helper Python dependencies")
    for node in manifest["custom_nodes"]:
        if not groups.intersection(node["groups"]):
            continue
        target = destination(root / "custom_nodes", node["name"])
        marker = target / ".castworkshop-install.json"
        try:
            owned = marker.exists() and json.loads(marker.read_text(encoding="utf-8")).get("revision") == node["revision"]
            if target.exists() and not owned:
                print(f"[setup] Existing node preserved: {node['name']} (manage its dependencies with ComfyUI Manager)")
                continue
            if not python and node["requirements"]:
                raise RuntimeError(f"{node['name']}: ComfyUI Python is required to install this node")
            # Resolve/install requirements before exposing new node code to ComfyUI.
            if node["requirements"]:
                install_requirements(python, node["requirements"])
            if not target.exists():
                with tempfile.TemporaryDirectory(prefix="castworkshop-node-download-") as temporary:
                    archive = Path(temporary) / (node["name"] + ".zip")
                    download(node, archive)
                    extract_node(archive, target)
                marker.write_text(json.dumps({"revision": node["revision"]}), encoding="utf-8")
                installed_nodes = True
            print(f"[setup] Node ready: {node['name']} at {node['revision'][:12]}")
        except Exception as exc:
            # Exceptions may carry signed URLs or credentials. Show only controlled errors.
            detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            print(f"[setup][warn] {node['name']}: {detail}")
            failures.append(node["name"])
    for asset in manifest["assets"]:
        if asset["group"] not in groups:
            continue
        try:
            present = find_present_model(models, asset, runtime_paths)
            if present:
                print(f"[setup] Already installed (name/size): {present}")
                continue
            download(asset, model_download_target(models, asset, runtime_paths, bool(models_dir)))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            print(f"[setup][warn] {asset['destination']}: {detail}")
            print(f"[setup] Publisher/download instructions: {asset['source']}")
            failures.append(asset["destination"])
    ffmpeg = resolve_ffmpeg(python) if "video" in groups else ""
    if "video" in groups:
        if ffmpeg:
            print(f"[setup] FFmpeg ready: {ffmpeg}")
        else:
            print("[setup][warn] FFmpeg unavailable on this platform; install it or set FFMPEG_PATH for video continuation")
            failures.append("FFmpeg")
    if installed_nodes:
        print("[setup] Custom nodes installed. Restart ComfyUI to load them; setup does not stop running jobs.")
    if "images" in groups:
        print("[setup] SNOFS is included in the image dependencies; its publisher's license applies.")
    if failures:
        print("[setup] Dependencies still requiring attention: " + ", ".join(failures))
    else:
        print("[setup] Selected downloads and node installations completed")
    check_nodes(groups, comfy_url or os.environ.get("COMFY_URL", "http://127.0.0.1:8188"))
    return {"failures": failures, "python": str(python) if python else "", "ffmpeg": ffmpeg,
            "restart_required": installed_nodes}


def describe_groups(groups=GROUPS, manifest=None):
    manifest = manifest or load_dependencies()
    print("[setup] Downloads go directly from publishers to your ComfyUI installation; their licenses apply separately from CastWorkshop's Apache license.")
    for group in groups:
        assets = [a for a in manifest["assets"] if a["group"] == group]
        label = "Image models" if group == "images" else "Video and MOSS voice models"
        print(f"[setup] {label}: {sum(a['size'] for a in assets) / 1e9:.1f} GB before skipping existing files")
        for url in sorted({a["license_url"] for a in assets}):
            print("  Publisher terms: " + url)
        print("  Node packages: " + ", ".join(n["name"] for n in manifest["custom_nodes"] if group in n["groups"]))
    print("[setup] Video generation also needs the image models for its starting image. SNOFS is included in the image group.")


def offer_install(comfy_path, comfy_url=None):
    import sys
    if not comfy_path:
        print("[setup] ComfyUI dependency installation skipped: set COMFYUI_PATH and rerun setup")
        return None
    manifest = load_dependencies()
    describe_groups(manifest=manifest)
    if not sys.stdin.isatty():
        print("[setup] Non-interactive setup: use scripts/comfy_setup.py --images and/or --video to opt in")
        return None
    groups = []
    for group, label in (("images", "image models and image custom nodes"), ("video", "video + MOSS voice models and video/audio custom nodes")):
        if input(f"  Install {label} under the publishers' terms? [y/N]: ").strip().lower() in ("y", "yes"):
            groups.append(group)
    if groups:
        python = find_comfy_python(comfy_path)
        if not python:
            entered = input("  ComfyUI Python executable (blank to download models only): ").strip().strip('"')
            python = entered or None
        return install_groups(comfy_path, groups, python=python, comfy_url=comfy_url)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", action="store_true")
    parser.add_argument("--video", action="store_true", help="Includes MOSS voice models")
    parser.add_argument("--list", action="store_true", help="Show downloads and publisher terms without installing")
    parser.add_argument("--check-only", action="store_true", help="Check registered workflow nodes without installing")
    parser.add_argument("--comfyui-path")
    parser.add_argument("--comfy-python")
    parser.add_argument("--models-dir", help="Override model storage for shared ComfyUI installations")
    args = parser.parse_args()
    try:
        from scripts.runtime import load_dotenv
    except ModuleNotFoundError:
        from runtime import load_dotenv
    load_dotenv()
    groups = [g for g in GROUPS if getattr(args, g)]
    if args.list:
        describe_groups(groups or GROUPS)
        return 0
    if args.check_only:
        return 0 if check_nodes(groups or GROUPS, os.environ.get("COMFY_URL", "http://127.0.0.1:8188")) else 1
    root = args.comfyui_path or os.environ.get("COMFYUI_PATH")
    if not root or not groups:
        parser.error("Specify --comfyui-path (or COMFYUI_PATH) and --images and/or --video")
    describe_groups(groups)
    result = install_groups(root, groups, python=args.comfy_python, models_dir=args.models_dir)
    # Direct execution places scripts/, rather than the project root, on sys.path.
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from setup_project import generate_env_file
    if (ROOT / ".env").exists():
        generate_env_file(str(Path(root).resolve()), comfy_python=result["python"], ffmpeg=result["ffmpeg"])
    else:
        print("[setup] Run setup_project.py to save detected helper paths in .env")
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
