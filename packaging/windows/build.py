#!/usr/bin/env python3
"""Cross-package the Windows app and compile a per-user offline NSIS installer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from prepare import BUILD, ROOT, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-install', action='store_true', help='Use existing pinned node_modules')
    args = parser.parse_args()
    npm = 'npm.cmd' if sys.platform == 'win32' else 'npm'
    if not args.skip_install:
        subprocess.run([npm, 'ci', '--no-audit', '--no-fund'], cwd=ROOT / 'windows', check=True)
    prepare()
    subprocess.run([npm, 'run', 'test'], cwd=ROOT / 'windows', check=True,
                   env={**os.environ, 'CODEX_DAG_TEST_PYTHON': sys.executable})
    subprocess.run([npm, 'run', 'package:win'], cwd=ROOT / 'windows', check=True)
    bundle = ROOT / 'dist/windows/win-unpacked'
    version = json.loads((ROOT / 'windows/package.json').read_text(encoding='utf-8'))['version']
    target = ROOT / 'dist' / f'Codex-DAG-{version}-Setup-x64.exe'
    compiler_root = BUILD / 'nsis'
    compiler = compiler_root / ('Bin/makensis.exe' if sys.platform == 'win32' else 'mac/makensis' if sys.platform == 'darwin' else 'linux/makensis')
    env = {**os.environ, 'NSISDIR': str(compiler_root)}
    define_prefix = '/D' if sys.platform == 'win32' else '-D'
    command = [str(compiler), '/WX' if sys.platform == 'win32' else '-WX', define_prefix + 'VERSION=' + version,
               define_prefix + 'BUNDLEDIR=' + str(bundle), define_prefix + 'OUTFILE=' + str(target),
               str(ROOT / 'packaging/windows/installer.nsi')]
    subprocess.run(command, env=env, check=True)
    subprocess.run([sys.executable, str(ROOT / 'packaging/windows/verify.py'), str(target)], check=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.exe.sha256').write_text(f'{digest}  {target.name}\n', encoding='utf-8')
    print(json.dumps({'installer': str(target), 'bytes': target.stat().st_size,
                      'sha256': digest, 'signing': 'unsigned', 'windows_execution': 'not performed'}))


if __name__ == '__main__':
    main()
