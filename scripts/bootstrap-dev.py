#!/usr/bin/env python3
"""Prepare isolated development data for the source-mode monitor.

The installed application never calls this helper; it starts empty in its own
application data folder. Existing development data is validated, never replaced.
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
MONITOR_DIR = PROJECT_DIR / "src" / "monitor"
sys.path.insert(0, str(MONITOR_DIR))
import monitor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_DIR / ".local/dev-data")
    parser.add_argument("--codex-dir", type=Path)
    parser.add_argument("--journal-dir", type=Path)
    parser.add_argument("--shared-journal", action="store_true")
    args = parser.parse_args()
    had_config = (args.data_dir.expanduser().resolve() / "config.json").exists()
    # configure_runtime also moves this data folder's own earlier ledger into a
    # shared journal when sharing is enabled, keeping the original bytes.
    data = monitor.configure_runtime(args.data_dir, args.codex_dir, MONITOR_DIR, args.journal_dir, args.shared_journal)
    # Reject damaged evidence instead of replacing it. An empty ledger is valid.
    monitor.snapshot()
    config = data / "config.json"
    if not had_config:
        settings = monitor.journal.load_config(data)
        settings.setdefault("project_path", str(PROJECT_DIR))
        settings.setdefault("root_session_id", None)
        monitor.journal.atomic_write(config, (monitor.journal.canonical(settings) + "\n").encode("utf-8"))
    print(f"Codex DAG 개발 데이터: {data} · 공개 저널: {monitor.LOG.parent}", file=sys.stderr)


if __name__ == "__main__":
    main()
