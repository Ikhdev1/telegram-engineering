"""Verify self-built skill/source archives and check the extracted skill offline.

Only run on archives you just built from trusted source: the extracted checker
is code. This is not a sandbox for arbitrary downloaded ZIP files.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def verify(directory: Path) -> dict:
    counts = {}
    for filename, source in (('telegram-engineering.skill', False), ('telegram-engineering-source.zip', True)):
        archive = directory / filename
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if bundle.testzip() is not None or 'telegram-engineering/SKILL.md' not in names:
                raise ValueError('Archive integrity or root skill missing')
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or '\\' in name or path.parts[0] != 'telegram-engineering':
                    raise ValueError('Unsafe archive member')
            if len(names) != len(set(names)):
                raise ValueError('Duplicate archive member')
            if any(name.endswith('/PROJECT_STATE.md') or '/__pycache__/' in name or name.endswith('/.env') for name in names):
                raise ValueError('Local runtime state in archive')
            if source and not all(any(name.startswith('telegram-engineering/' + prefix) for name in names) for prefix in ('tests/', 'evals/', '.github/workflows/')):
                raise ValueError('Source archive is incomplete')
            if not source and any(name.startswith(('telegram-engineering/tests/', 'telegram-engineering/evals/', 'telegram-engineering/.github/')) for name in names):
                raise ValueError('Compact skill includes source-only files')
            counts[filename] = len(names)
            if not source:
                with tempfile.TemporaryDirectory() as temporary:
                    bundle.extractall(temporary)
                    root = Path(temporary) / 'telegram-engineering'
                    subprocess.run([sys.executable, '-X', 'utf8', str(root / 'scripts' / 'check_skill.py'), '--root', str(root), '--json'], check=True)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        counts = verify(args.directory.resolve())
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Archive verification failed: {type(error).__name__}\n')
    print('Archives verified: ' + ', '.join(f'{name}: {count} files' for name, count in counts.items()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
