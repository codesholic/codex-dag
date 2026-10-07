#!/usr/bin/env python3
"""Repeatable arm64 app + DMG build; only allowlisted resources are shipped."""
import argparse
import importlib.metadata
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
DIST = ROOT / "dist"


def run(*command):
    subprocess.run([str(p) for p in command], check=True, cwd=ROOT)


def audit_binaries(app):
    binaries, minimums = 0, []
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        kind = subprocess.check_output(["/usr/bin/file", "-b", str(path)], text=True)
        if "Mach-O" not in kind:
            continue
        binaries += 1
        architectures = subprocess.check_output(["/usr/bin/lipo", "-archs", str(path)], text=True).split()
        if architectures != ["arm64"]:
            raise RuntimeError(f"Unexpected architectures in {path.name}: {architectures}")
        headers = subprocess.check_output(["/usr/bin/otool", "-l", str(path)], text=True)
        minimums += re.findall(r"\bminos\s+(\d+(?:\.\d+)*)", headers)
        linked = subprocess.check_output(["/usr/bin/otool", "-L", str(path)], text=True).splitlines()[1:]
        for line in linked:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
                raise RuntimeError(f"External dependency in {path.name}: {dependency}")
    highest = max(minimums, key=lambda s: tuple(map(int, s.split("."))), default="0")
    if tuple((list(map(int, highest.split("."))) + [0, 0, 0])[:3]) > (15, 0, 0):
        raise RuntimeError(f"A bundled runtime requires macOS {highest}; declared minimum is 15.0")
    return {"mach_o_count": binaries, "highest_runtime_minimum": highest,
            "external_non_system_dependencies": 0}


def sign(app, identity):
    """Sign from inner Mach-O binaries outward, without codesign --deep signing."""
    entitlements = ROOT / "packaging/macos/backend.entitlements.plist"
    for path in sorted(app.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or not path.is_file():
            continue
        kind = subprocess.check_output(["/usr/bin/file", "-b", str(path)], text=True)
        if "Mach-O" in kind:
            args = ["/usr/bin/codesign", "--force", "--sign", identity]
            if identity != "-":
                args += ["--options", "runtime", "--timestamp"]
            if path.name == "codex-dag-backend":
                args += ["--entitlements", str(entitlements)]
            run(*args, path)
    frameworks = sorted(app.rglob("*.framework"), key=lambda p: len(p.parts), reverse=True)
    for path in frameworks:
        run("/usr/bin/codesign", "--force", "--sign", identity, path)
    args = ["/usr/bin/codesign", "--force", "--sign", identity]
    if identity != "-":
        args += ["--options", "runtime", "--timestamp"]
    run(*args, app)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.1.8")
    parser.add_argument("--identity", default=os.environ.get("CODEX_DAG_SIGN_IDENTITY", "-"))
    parser.add_argument("--skip-dmg", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("Build requires a native Apple Silicon macOS Python")
    pyinstaller_version = importlib.metadata.version("pyinstaller")
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--target-arch", "arm64", "--name", "codex-dag-backend",
        "--distpath", BUILD / "backend-dist", "--workpath", BUILD / "pyinstaller",
        "--specpath", BUILD, "--paths", ROOT / "src/monitor",
        "--add-data", str(ROOT / "src/monitor/index.html") + ":.",
        ROOT / "src/monitor/monitor.py")
    app = DIST / "Codex DAG.app"
    # Only this generated output directory is replaced. No installed app is touched.
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    resources = contents / "Resources"
    executable = contents / "MacOS/Codex DAG"
    executable.parent.mkdir(parents=True)
    resources.mkdir()
    shutil.copytree(BUILD / "backend-dist/codex-dag-backend", resources / "backend", symlinks=True)
    sources = sorted((ROOT / "macos").rglob("*.swift"))
    if not sources:
        parser.error("Native Swift sources are missing")
    run("/usr/bin/xcrun", "swiftc", "-O", "-whole-module-optimization",
        "-target", "arm64-apple-macosx15.0", "-framework", "AppKit",
        "-framework", "WebKit", "-framework", "ServiceManagement",
        *sources, "-o", executable)
    info = {
        "CFBundleDevelopmentRegion": "ko", "CFBundleDisplayName": "Codex DAG",
        "CFBundleName": "Codex DAG", "CFBundleExecutable": "Codex DAG",
        "CFBundleIdentifier": "io.github.codesholic.codex-dag", "CFBundlePackageType": "APPL",
        "NSHumanReadableCopyright": "© 2026 codesholic",
        "CFBundleShortVersionString": args.version, "CFBundleVersion": args.version,
        "LSMinimumSystemVersion": "15.0", "LSUIElement": True,
        "NSHighResolutionCapable": True, "LSMultipleInstancesProhibited": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    licenses = resources / "Licenses"
    licenses.mkdir()
    # Locate bundled interpreter license across Python patch/minor versions.
    candidates = list(Path(sys.base_prefix).glob("lib/python*/LICENSE.txt"))
    if candidates:
        shutil.copyfile(candidates[0], licenses / "Python.txt")
    distribution = importlib.metadata.distribution("pyinstaller")
    for entry in distribution.files or []:
        if "COPYING" in str(entry) or ("licenses/" in str(entry) and entry.name.lower().startswith("license")):
            source = Path(distribution.locate_file(entry))
            if source.is_file():
                shutil.copyfile(source, licenses / ("PyInstaller-" + source.name))
    status = "ad-hoc local development" if args.identity == "-" else "Developer ID signed; notarization pending"
    (resources / "build-info.json").write_text(json.dumps({
        "version": args.version, "architecture": "arm64", "minimum_macos": "15.0",
        "python": platform.python_version(), "pyinstaller": pyinstaller_version,
        "signing": status, "notarized": False}, indent=2) + "\n")
    audit = audit_binaries(app)
    (BUILD / "package-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    sign(app, args.identity)
    # Fail closed if any private/development state entered the allowlisted package.
    forbidden = {"config.json", "events.jsonl", ".server.json", ".local", ".codex", "HANDOFF.md"}
    leaked = [str(p.relative_to(app)) for p in app.rglob("*") if p.name in forbidden]
    if leaked:
        raise RuntimeError("Unexpected private resources: " + repr(leaked))
    dmg = DIST / "Codex DAG.dmg"
    if not args.skip_dmg:
        stage = BUILD / "dmg-stage"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        (stage / "설치 안내.txt").write_text(
            "Codex DAG.app을 Applications에 복사한 후 실행하세요.\n"
            "Apple Silicon / macOS 15 이상. Python 설치 불필요.\n"
            f"빌드 상태: {status}.\n"
            "메뉴바에서 모니터 창과 설정을 열 수 있습니다.\n"
            "로컬 개발 빌드는 Apple 공증을 받지 않았습니다.\n")
        run("/usr/bin/hdiutil", "create", "-ov", "-format", "UDZO", "-volname",
            "Codex DAG", "-srcfolder", stage, dmg)
    print(json.dumps({"app": str(app), "dmg": str(dmg) if not args.skip_dmg else None,
                      "signing": status, "notarized": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
