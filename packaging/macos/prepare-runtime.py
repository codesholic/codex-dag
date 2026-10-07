#!/usr/bin/env python3
"""Extract a pinned PSF runtime locally; never runs the system installer."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
VERSION = "3.13.5"
SHA256 = "e754d6cae3f2810dd1818c1395a7ff50ce79ade3ec4887f7ce23ad581cc12e3a"


def run(*args, **kwargs):
    return subprocess.run([str(v) for v in args], check=True, **kwargs)


def main():
    LOCAL.mkdir(exist_ok=True)
    package = LOCAL / f"python-{VERSION}-macos11.pkg"
    if not package.exists():
        run("/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
            f"https://www.python.org/ftp/python/{VERSION}/{package.name}", "-o", package)
    if hashlib.sha256(package.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("PSF installer checksum mismatch; preserve it for inspection")
    signature = subprocess.check_output(["/usr/sbin/pkgutil", "--check-signature", str(package)], text=True)
    if "Developer ID Installer: Python Software Foundation (BMM5U3QVKW)" not in signature:
        raise RuntimeError("Unexpected runtime publisher")
    expanded = LOCAL / "python-runtime-expanded"
    if not expanded.exists():
        run("/usr/sbin/pkgutil", "--expand-full", package, expanded)
    framework = LOCAL / "build-runtime/Python.framework"
    marker = LOCAL / "build-runtime/prepared.json"
    if not marker.exists():
        if framework.exists():
            shutil.rmtree(framework)  # Only this generated, local runtime output.
        shutil.copytree(expanded / "Python_Framework.pkg/Payload", framework, symlinks=True)
        prefix = "/Library/Frameworks/Python.framework/"
        for path in framework.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if "Frameworks" in path.relative_to(framework).parts:
                continue  # Tcl/Tk is unused by the backend and not packaged.
            if path.suffix not in {".so", ".dylib"} and path.name not in {"Python", "python3.13", "python3.13-intel64"}:
                continue
            kind = subprocess.check_output(["/usr/bin/file", "-b", str(path)], text=True)
            if "Mach-O" not in kind:
                continue
            linked = subprocess.check_output(["/usr/bin/otool", "-L", str(path)], text=True).splitlines()[1:]
            changed = False
            for line in linked:
                old = line.strip().split(" (", 1)[0]
                if old.startswith(prefix):
                    target = framework / old[len(prefix):]
                    if target.resolve() == path.resolve():
                        continue  # A dylib install ID is not a load command.
                    replacement = "@loader_path/" + os.path.relpath(target, path.parent)
                    run("/usr/bin/install_name_tool", "-change", old, replacement, path,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    changed = True
            if changed:
                run("/usr/bin/codesign", "--force", "--sign", "-", path,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marker.write_text(json.dumps({"python": VERSION, "source_sha256": SHA256,
                                     "publisher": "Python Software Foundation", "installed_systemwide": False}) + "\n")
    python = framework / "Versions/3.13/bin/python3.13"
    run(python, "-c", "import ssl,sqlite3,lzma; print('PSF runtime ready')")
    venv = LOCAL / "build-venv"
    if not (venv / "bin/python").exists():
        run(python, "-m", "venv", venv)
    wheels = LOCAL / "build-wheels"
    wheels.mkdir(exist_ok=True)
    requirements = ROOT / "packaging/macos/requirements-build.txt"
    run(sys.executable, "-m", "pip", "download", "--only-binary=:all:", "-r", requirements, "-d", wheels)
    run(venv / "bin/python", "-m", "pip", "install", "--no-index", "--find-links", wheels, "-r", requirements)
    print(f"Build runtime: {venv / 'bin/python'}")


if __name__ == "__main__":
    main()
