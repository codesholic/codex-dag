"""Shared storage boundary checks. All Codex and bundled inputs stay read-only."""
from pathlib import Path


def validate_storage(data_directory, codex_directory, resource_directory):
    data, codex, resources = (Path(p).expanduser().resolve() for p in
                              (data_directory, codex_directory, resource_directory))
    for label, source in (("Codex input", codex), ("bundled resources", resources)):
        if data == source or source in data.parents:
            raise ValueError(f"Data directory cannot be inside {label}: {source}")
    if any(path.suffix.lower() == ".app" for path in (data, *data.parents)):
        raise ValueError("Data directory cannot be inside an application bundle")
    return data, codex, resources
