"""Build the MLX 8080 macOS app from this checkout; model weights stay external."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import plistlib
import platform
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
PACKAGE = ROOT / "src/mlx_activation_lab"
IDENTIFIER = "local.yukinco.mlx-server-8080"

def run(*args: str | Path) -> None:
    subprocess.run([str(arg) for arg in args], check=True)

def make_icon(generator: Path, mode: str, output: Path, scratch: Path) -> None:
    base = scratch / f"{mode}-1024.png"
    run(generator, mode, base)
    iconset = scratch / f"{mode}.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            filename = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            run("/usr/bin/sips", "-z", str(pixels), str(pixels), base, "--out", iconset / filename)
    run("/usr/bin/iconutil", "--convert", "icns", "--output", output, iconset)

def build(output: Path, python: Path, server_root: Path, gguf_root: Path | None) -> Path:
    # Do not resolve the Python symlink: its venv path selects installed packages.
    python = python.expanduser().absolute()
    server_root = server_root.expanduser().resolve()
    output = output.expanduser().resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"Pythonが実行できません: {python}")
    if not (server_root / "start_server.sh").is_file():
        raise SystemExit(f"起動スクリプトがありません: {server_root}")
    if gguf_root is not None:
        gguf_root = gguf_root.expanduser().resolve()
        if not (gguf_root / "server.py").is_file():
            raise SystemExit(f"GGUF連携先にserver.pyがありません: {gguf_root}")
    output.mkdir(parents=True, exist_ok=True)
    target = output / "MLX 8080.app"
    backup = output / ".MLX 8080.backup.app"
    if backup.exists():
        raise SystemExit(f"前回のバックアップを確認してください: {backup}")
    if target.exists():
        info = target / "Contents/Info.plist"
        if not info.is_file() or plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") != IDENTIFIER:
            raise SystemExit(f"既存の別アプリは上書きしません: {target}")
        running = subprocess.run(["/usr/bin/pgrep", "-f", str(target / "Contents/MacOS/MLXAppShell")],
                                 capture_output=True, text=True)
        if running.returncode != 1:
            raise SystemExit(f"アプリの終了を確認してから再ビルドしてください: {target}")
    # Stage on the destination filesystem so the final rename is atomic.
    with tempfile.TemporaryDirectory(prefix=".mlx-build-", dir=output) as temporary:
        scratch = Path(temporary)
        contents = scratch / "MLX 8080.app/Contents"
        binaries = contents / "MacOS"
        resources = contents / "Resources"
        binaries.mkdir(parents=True)
        resources.mkdir()
        cache = scratch / "module-cache"
        for source, executable, frameworks in [
            ("WebAppShell.swift", binaries / "MLXAppShell", ["AppKit", "WebKit"]),
            ("ServerManager.swift", binaries / "MLXServerManager", ["Network", "Security"]),
            ("MakeIcon.swift", scratch / "MakeIcon", ["AppKit"]),
        ]:
            flags = [value for framework in frameworks for value in ("-framework", framework)]
            run("/usr/bin/swiftc", "-swift-version", "5", "-O",
                "-target", f"{platform.machine()}-apple-macos14.0", "-module-cache-path", cache,
                *flags, NATIVE / source, "-o", executable)
        shutil.copytree(PACKAGE, resources / "src/mlx_activation_lab",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        make_icon(scratch / "MakeIcon", "server", resources / "AppIcon.icns", scratch)
        info = {
            "CFBundleName": "MLX 8080", "CFBundleDisplayName": "MLX 8080",
            "CFBundleExecutable": "MLXAppShell", "CFBundleIdentifier": IDENTIFIER,
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.1",
            "CFBundleVersion": "2", "CFBundleIconFile": "AppIcon",
            "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            "MLXBackendIdentity": "mlx-server-manager", "MLXBackendPort": 8766,
            "MLXPythonPath": str(python), "MLXServerRoot": str(server_root),
        }
        if gguf_root is not None:
            info["MLXGGUFRoot"] = str(gguf_root)
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        run("/usr/bin/codesign", "--force", "--sign", "-", "--deep", contents.parent)
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", contents.parent)
        if target.exists():
            target.rename(backup)
        try:
            contents.parent.rename(target)
        except Exception:
            if backup.exists():
                backup.rename(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    print(target)
    return target

def main():
    parser = argparse.ArgumentParser(description="Build MLX 8080.app")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python3")
    parser.add_argument("--server-root", type=Path, default=ROOT)
    parser.add_argument("--gguf-root", type=Path,
                        help="Optional external GGUF launcher directory (see docs/gguf-integration.md)")
    args = parser.parse_args()
    build(args.output, args.python, args.server_root, args.gguf_root)

if __name__ == "__main__":
    main()
