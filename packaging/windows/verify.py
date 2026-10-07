#!/usr/bin/env python3
"""Inspect actual PE binaries, runtime hashes and private-data exclusions.

This is a cross-build inspection, never a claim of Windows execution.
"""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def pe(path):
    data = path.read_bytes()
    if data[:2] != b'MZ':
        raise ValueError(f'Not a Windows PE: {path.name}')
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    if data[offset:offset+4] != b'PE\0\0':
        raise ValueError(f'Invalid PE signature: {path.name}')
    machine = struct.unpack_from('<H', data, offset + 4)[0]
    optional = offset + 24
    magic = struct.unpack_from('<H', data, optional)[0]
    directories = optional + (112 if magic == 0x20b else 96)
    certificate_offset, certificate_size = struct.unpack_from('<II', data, directories + 8 * 4)
    return {'machine': hex(machine), 'bytes': len(data), 'certificate_bytes': certificate_size}


def main():
    installer = Path(sys.argv[1])
    bundle = ROOT / 'dist/windows/win-unpacked'
    resources = bundle / 'resources'
    info = json.loads((resources / 'build-info.json').read_text(encoding='utf-8'))
    checks = {'installer': pe(installer), 'app': pe(bundle / 'Codex DAG.exe'),
              'python': pe(resources / 'python/python.exe')}
    if checks['app']['machine'] != '0x8664' or checks['python']['machine'] != '0x8664':
        raise ValueError('App and Python must both be Windows x64')
    if checks['installer']['certificate_bytes']:
        raise ValueError('Expected unsigned development installer; signing status must be updated')
    for name, expected in info['files'].items():
        actual = resources / name
        if hashlib.sha256(actual.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Runtime hash mismatch: {name}')
    for source in (ROOT/'src/monitor').iterdir():
        if source.name in {'monitor.py','collector.py','codex_context.py','storage.py','platform_runtime.py','journal.py','index.html'}:
            if source.read_bytes() != (resources/'backend'/source.name).read_bytes():
                raise ValueError(f'Backend is stale: {source.name}')
    forbidden = {'.local', 'config.json', 'events.jsonl', '.server.json', 'desktop-settings.json', 'node_modules', '__pycache__'}
    for item in resources.rglob('*'):
        if item.name in forbidden or item.suffix.lower() in {'.jsonl', '.p12', '.pfx', '.key'}:
            raise ValueError(f'Private or development data in bundle: {item.relative_to(bundle)}')
    expected_backend = {'monitor.py','collector.py','codex_context.py','storage.py','platform_runtime.py','journal.py','index.html'}
    if {p.name for p in (resources/'backend').iterdir()} != expected_backend:
        raise ValueError('Backend bundle does not match explicit source allowlist')
    embedded_paths = next((resources/'python').glob('python*._pth')).read_text(encoding='utf-8').splitlines()
    if '..\\backend' not in embedded_paths and '../backend' not in embedded_paths:
        raise ValueError('Isolated Python cannot import the app backend')
    if 'import site' in embedded_paths or any('/Users/' in value for value in embedded_paths):
        raise ValueError('Embedded Python path isolation was changed')
    # Electron ASAR files are inspected using the same pinned parser as the builder.
    import subprocess
    listing = subprocess.check_output(['node', '-e',
        "const a=require('@electron/asar'); process.stdout.write(JSON.stringify(a.listPackage(process.argv[1])))",
        str(resources/'app.asar')], cwd=ROOT/'windows', text=True)
    app_files = json.loads(listing)
    for name in app_files:
        if any(part in forbidden for part in Path(name.lstrip('/')).parts) or name.endswith('.jsonl') or '/test/' in name:
            raise ValueError(f'Development file inside ASAR: {name}')
    hashes = json.loads(subprocess.check_output(['node', '-e',
        "const a=require('@electron/asar'),c=require('crypto'); let files=a.listPackage(process.argv[1]).filter(n=>n.endsWith('.cjs')||n.endsWith('.html')||n.startsWith('/assets/')); process.stdout.write(JSON.stringify(Object.fromEntries(files.map(n=>[n.slice(1),c.createHash('sha256').update(a.extractFile(process.argv[1],n.slice(1))).digest('hex')]))))",
        str(resources/'app.asar')], cwd=ROOT/'windows', text=True))
    for name, digest in hashes.items():
        if hashlib.sha256((ROOT/'windows'/name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'Host source is stale in ASAR: {name}')
    # Inspect the actual NSIS payload, not only the directory that fed the compiler.
    extractor = subprocess.check_output(['node', '-e',
        "require('app-builder-lib/out/toolsets/7zip').getPath7za().then(p=>process.stdout.write(p))"],
        cwd=ROOT/'windows', text=True).strip()
    with tempfile.TemporaryDirectory(prefix='codex-dag-installer-check-') as temp:
        subprocess.run([extractor, 'x', '-y', f'-o{temp}', str(installer.resolve()), 'resources/*', 'Codex DAG.exe', '-r'],
                       check=True, stdout=subprocess.DEVNULL)
        extracted = Path(temp)
        for resource in resources.rglob('*'):
            if resource.is_file():
                payload = extracted / resource.relative_to(bundle)
                if not payload.is_file() or hashlib.sha256(payload.read_bytes()).digest() != hashlib.sha256(resource.read_bytes()).digest():
                    raise ValueError(f'Installer payload differs from audited bundle: {resource.relative_to(bundle)}')
        if (extracted/'Codex DAG.exe').read_bytes() != (bundle/'Codex DAG.exe').read_bytes():
            raise ValueError('Installer executable differs from audited x64 app')
    result = {'product':'Codex DAG','version':info['version'],'platform':'Windows x64',
              'checks':checks,'runtime_files_verified':len(info['files']),'asar_files':app_files,'host_hashes':hashes,
              'installer_sha256':hashlib.sha256(installer.read_bytes()).hexdigest(),
              'installer_payload_matches_audited_bundle':True,
              'signing':'installer unsigned','windows_install_or_execution':'not performed'}
    (ROOT/'dist/windows/package-audit.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
