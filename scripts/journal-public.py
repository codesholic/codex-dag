#!/usr/bin/env python3
"""Record an exact public message through the shared-journal CLI resolver."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/monitor'))
import monitor

arguments = sys.argv[1:]
if '--message-file' in arguments:
    index = arguments.index('--message-file')
    if index + 1 >= len(arguments):
        raise SystemExit('--message-file requires a UTF-8 file')
    body = Path(arguments[index+1]).read_text(encoding='utf-8')
    arguments[index:index+2] = ['--message', body]
# Runtime path arguments belong before the message subcommand. The shared store
# remains configurable via --journal-dir/environment/persisted runtime settings.
global_args = ['--shared-journal']
for flag in ['--data-dir','--codex-dir','--resource-dir','--journal-dir']:
    if flag in arguments:
        index = arguments.index(flag)
        if index+1 >= len(arguments):raise SystemExit(f'{flag} requires a path')
        global_args += arguments[index:index+2];del arguments[index:index+2]
sys.argv = [str(ROOT/'src/monitor/monitor.py'), *global_args, 'message', *arguments]
monitor.main()
