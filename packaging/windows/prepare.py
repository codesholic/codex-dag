#!/usr/bin/env python3
"""Prepare a clean Windows x64 runtime using pinned official binary archives."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / 'build/windows'
LOCK = json.loads((Path(__file__).with_name('runtime-lock.json')).read_text(encoding='utf-8'))
BACKEND_FILES = ('monitor.py', 'collector.py', 'codex_context.py', 'storage.py', 'platform_runtime.py', 'journal.py', 'index.html')


def download(name):
    item = LOCK[name]
    cache = ROOT / '.local/windows-downloads' / item['url'].rsplit('/', 1)[-1]
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        temporary = cache.with_suffix(cache.suffix + '.part')
        with urllib.request.urlopen(item['url'], timeout=60) as source, temporary.open('wb') as target:
            shutil.copyfileobj(source, target)
        temporary.replace(cache)
    if hashlib.sha256(cache.read_bytes()).hexdigest() != item['sha256']:
        raise ValueError(f'{name} archive checksum mismatch: {cache}')
    return cache


def prepare():
    BUILD.mkdir(parents=True, exist_ok=True)
    runtime = BUILD / 'python'
    backend = BUILD / 'backend'
    for directory in (runtime, backend):
        if directory.exists():
            shutil.rmtree(directory)  # Only this script's generated output.
        directory.mkdir()
    with zipfile.ZipFile(download('python')) as archive:
        for name in archive.namelist():
            if Path(name).is_absolute() or '..' in Path(name).parts:
                raise ValueError('Invalid archive member')
        archive.extractall(runtime)
    # Isolated Python search paths never consult the selected project or user site.
    paths = list(runtime.glob('python*._pth'))
    if len(paths) != 1:
        raise ValueError('Expected one embedded Python path configuration')
    stdlib = next(runtime.glob('python[0-9]*.zip')).name
    paths[0].write_text(f'{stdlib}\n.\n../backend\n', encoding='utf-8')
    for name in BACKEND_FILES:
        source = ROOT / 'src/monitor' / name
        if not source.is_file():
            raise ValueError(f'Missing backend resource: {name}')
        shutil.copyfile(source, backend / name)
    files = {str(p.relative_to(BUILD)): hashlib.sha256(p.read_bytes()).hexdigest()
             for directory in (runtime, backend) for p in sorted(directory.rglob('*')) if p.is_file()}
    version = json.loads((ROOT / 'windows/package.json').read_text(encoding='utf-8'))['version']
    info = {'product': 'Codex DAG', 'version': version, 'platform': 'win32', 'architecture': 'x64',
            'python_version': LOCK['python']['version'], 'runtime_source': LOCK['python']['url'],
            'runtime_sha256': LOCK['python']['sha256'], 'signing': 'unsigned', 'files': files}
    (BUILD / 'build-info.json').write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
    compiler = BUILD / 'nsis'
    compiler.mkdir(exist_ok=True)
    nsis_archive = download('nsis')
    if sys.platform == 'win32':
        # The package builder already downloads 7zip; use its exported toolset.
        extractor = subprocess.check_output(['node', '-e',
            "require('app-builder-lib/out/toolsets/7zip').getPath7za().then(p=>process.stdout.write(p))"],
            cwd=ROOT / 'windows', text=True).strip()
        subprocess.run([extractor, 'x', '-y', f'-o{compiler}', str(nsis_archive)], check=True)
    else:
        subprocess.run(['tar', '-xf', str(nsis_archive), '-C', str(compiler)], check=True)
    print(json.dumps({'runtime': str(runtime), 'backend': str(backend), 'version': version}))


if __name__ == '__main__':
    prepare()
