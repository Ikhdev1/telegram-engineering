"""Build reproducible local archives; never upload or include runtime state."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_safety import contains_secret

EXCLUDED_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules', 'dist', 'build', 'data', '.idea', '.vscode'}
EXCLUDED_NAMES = {'PROJECT_STATE.md', '.DS_Store', 'Thumbs.db', '.coverage'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.sqlite', '.sqlite3', '.db', '.session', '.log', '.pem', '.key', '.p12', '.pfx', '.skill', '.zip'}
SKILL_ROOTS = {'SKILL.md', 'README.md', 'README.ru.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md', 'RELEASING.md', 'LICENSE', 'references', 'scripts', 'assets', 'examples', 'docs', 'requirements-dev.txt'}
SOURCE_REQUIRED = ('tests', 'evals', '.github/workflows/check.yml')


def distributable(relative: Path, kind: str) -> bool:
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return False
    if relative.name in EXCLUDED_NAMES or relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if relative.name.startswith('.env') and relative.name != '.env.example':
        return False
    if relative.name.endswith(('-wal', '-shm', '.egg-info')) or any(marker in relative.name.lower() for marker in ('.db-', '.sqlite-', '.sqlite3-', '.session-')):
        return False
    if any(part.endswith('.egg-info') for part in relative.parts):
        return False
    return kind == 'source' or relative.parts[0] in SKILL_ROOTS


def package(root: Path, output: Path, *, kind: str = 'skill') -> Path:
    root = Path(root).resolve()
    output = Path(output).resolve()
    if kind not in {'skill', 'source'}:
        raise ValueError('kind must be skill or source')
    if root == output or output in root.parents:
        raise ValueError('Output must not be the source root or an ancestor of it')
    if not (root / 'SKILL.md').is_file():
        raise ValueError('SKILL.md is required')
    name_match = re.search(r'^name:\s*([a-z0-9]+(?:-[a-z0-9]+)*)\s*$', (root / 'SKILL.md').read_text(encoding='utf-8'), re.M)
    if name_match is None or name_match[1] != root.name:
        expected = name_match[1] if name_match else 'the name in SKILL.md'
        raise ValueError(f'Rename the skill directory to {expected} before packaging')
    if kind == 'source' and any(not (root / entry).exists() for entry in SOURCE_REQUIRED):
        raise ValueError('Source distribution requires a full checkout with tests, evals and .github/workflows/check.yml')
    files = []
    for path in sorted(root.rglob('*'), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root)
        if path == output or output in path.parents:
            continue
        if not distributable(rel, kind):
            continue
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Links or paths outside the source are not distributable: {rel.as_posix()}')
        if path.is_file():
            if contains_secret(path.read_bytes()):
                raise ValueError(f'Possible credential in selected release file: {rel.as_posix()}')
            files.append((path, rel))
    if not files or not any(relative.as_posix() == 'SKILL.md' for _, relative in files):
        raise ValueError('Distribution must contain SKILL.md')
    output.mkdir(parents=True, exist_ok=True)
    suffix = '.skill' if kind == 'skill' else '-source.zip'
    destination = output / f'{root.name}{suffix}'
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for path, relative in files:
                info = zipfile.ZipInfo(f'{root.name}/{relative.as_posix()}', date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, path.read_bytes())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('dist'))
    parser.add_argument('--kind', choices=('skill', 'source'), default='skill')
    args = parser.parse_args()
    try:
        archive = package(args.root, args.output, kind=args.kind)
    except (ValueError, OSError) as exc:
        parser.exit(2, f'Packaging failed: {exc}\n')
    with zipfile.ZipFile(archive) as bundle:
        count = len(bundle.namelist())
    print(json.dumps({'archive': str(archive), 'files': count, 'bytes': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
