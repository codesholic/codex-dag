#!/usr/bin/env python3
"""Verify compiled native storage boundaries without launching NSApplication."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

binary = Path(sys.argv[1] if len(sys.argv) > 1 else 'build/native/Codex DAG').resolve()
with tempfile.TemporaryDirectory(prefix='codex-dag-native-storage-') as raw:
    root = Path(raw)
    codex = root / 'codex'
    codex.mkdir()
    (codex / 'preserve.txt').write_text('read-only source sentinel\n')
    alias = root / 'source-alias'
    alias.symlink_to(codex, target_is_directory=True)
    cases = [
        ('separate empty data', root / 'data', codex, 0),
        ('same source folder', codex, codex, 2),
        ('source child folder', codex / 'new-data', codex, 2),
        ('source via symlink', alias / 'new-data', codex, 2),
        ('source argument via symlink', codex / 'new-data', alias, 2),
        ('similar prefix is separate', root / 'codex-two', codex, 0),
        ('relative data path', Path('relative-data'), codex, 2),
        ('root source includes all data', root / 'data', Path('/'), 2),
        ('compiled resources folder', binary.parent / 'app-data', codex, 2),
    ]
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    results = []
    for title, data, source, expected in cases:
        result = subprocess.run([str(binary), '--validate-storage', str(data), str(source)], text=True, capture_output=True)
        assert result.returncode == expected, (title, result.returncode, result.stdout, result.stderr)
        assert not (codex / 'new-data').exists(), title
        results.append({'case': title, 'exit_code': result.returncode})
    after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    assert before == after, 'Read-only check wrote into the source'
    assert not (root / 'data').exists(), 'Read-only check created the data folder'
    print(json.dumps({'checks': len(cases) + 2, 'source_unchanged': True, 'data_not_created': True, 'results': results}, ensure_ascii=False, indent=2))
