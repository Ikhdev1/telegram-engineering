"""Offline skill health check: metadata, local links, JSON and Python syntax.

Does not fetch external URLs, execute code, scan the operator's environment or
claim that Telegram accepted a payload. Run the example smoke separately.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from urllib.parse import unquote

import sys

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_safety import contains_secret

import yaml

EXCLUDED = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'node_modules', 'dist', 'build', 'data'}
MACHINE_PATH = re.compile(r'(?i)(?:[a-z]:[\\/]Users[\\/][^\s`"<>]+|/home/[^\s`"<>]+|/Users/[^\s`"<>]+)')
LINK = re.compile(r'\[[^\]\n]+\]\(([^\s)]+)(?:\s+"[^"]*")?\)')


def _finding(code: str, path: str, message: str, severity: str = 'error') -> dict:
    return {'severity': severity, 'code': code, 'path': path, 'message': message}


def _frontmatter(root: Path) -> list[dict]:
    target = root / 'SKILL.md'
    if not target.is_file():
        return [_finding('missing-skill', 'SKILL.md', 'Required SKILL.md is missing.')]
    text = target.read_text(encoding='utf-8')
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        return [_finding('frontmatter', 'SKILL.md', 'Expected YAML frontmatter delimited by --- lines.')]
    try:
        fields = yaml.safe_load(match[1])
    except yaml.YAMLError:
        return [_finding('frontmatter', 'SKILL.md', 'Invalid YAML; quote descriptions containing colon-space.')]
    if not isinstance(fields, dict):
        return [_finding('frontmatter', 'SKILL.md', 'Frontmatter must be a mapping.')]
    findings = []
    name = fields.get('name')
    if not isinstance(name, str) or name != root.name or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        findings.append(_finding('skill-name', 'SKILL.md', 'name must be lowercase kebab-case and match the skill directory.'))
    description = fields.get('description')
    if not isinstance(description, str) or not description.startswith('Use when') or not 1 <= len(description) <= 1024:
        findings.append(_finding('description', 'SKILL.md', 'description must start with Use when and contain at most 1024 characters.'))
    if len(text.splitlines()) > 500:
        findings.append(_finding('core-length', 'SKILL.md', 'Move detailed material to linked references; core exceeds 500 lines.'))
    return findings


def _document(root: Path, path: Path) -> list[dict]:
    text = path.read_text(encoding='utf-8')
    rel = path.relative_to(root).as_posix()
    findings = []
    if MACHINE_PATH.search(text):
        findings.append(_finding('machine-path', rel, 'Replace operator-specific absolute paths with portable project-relative instructions.'))
    # Strip fenced code before extracting navigational links; code may explain syntax.
    plain = re.sub(r'(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1\s*$', '', text)
    for match in LINK.finditer(plain):
        link = unquote(match[1])
        if re.match(r'^[a-z][a-z0-9+.-]*:', link, re.I) or link.startswith(('#', '//')):
            continue
        target = (path.parent / link.split('#')[0].split('?')[0]).resolve()
        if not target.exists():
            findings.append(_finding('broken-link', rel, f'Local target does not exist: {link}'))
    open_fence = None
    language = ''
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^(`{3,}|~{3,})([^\n]*)', line)
        if open_fence is None and marker:
            open_fence = marker[1]
            language = marker[2].strip().split(' ', 1)[0]
            body = []
        elif open_fence is not None and marker and marker[1][0] == open_fence[0] and len(marker[1]) >= len(open_fence):
            if language == 'json':
                try:
                    json.loads(''.join(body))
                except json.JSONDecodeError:
                    findings.append(_finding('invalid-json-example', rel, 'Fenced JSON example is not valid JSON.'))
            open_fence = None
        elif open_fence is not None:
            body.append(line)
    if open_fence is not None:
        findings.append(_finding('unclosed-fence', rel, 'Code fence is not closed.'))
    return findings


def check(root: Path) -> list[dict]:
    root = Path(root).resolve()
    findings = _frontmatter(root)
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part in EXCLUDED or part.endswith('.egg-info') for part in relative.parts) or path.name == 'PROJECT_STATE.md' or not path.is_file():
            continue
        if contains_secret(path.read_bytes()):
            findings.append(_finding('possible-secret', relative.as_posix(), 'Possible Telegram token or private key in published content; remove and review locally.'))
        if path.suffix == '.md':
            findings.extend(_document(root, path))
        elif path.suffix == '.py':
            try:
                ast.parse(path.read_text(encoding='utf-8'), filename=relative.as_posix())
            except SyntaxError as exc:
                findings.append(_finding('python-syntax', relative.as_posix(), f'Python syntax error on line {exc.lineno}.'))
        elif path.suffix == '.json':
            try:
                json.loads(path.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                findings.append(_finding('invalid-json', relative.as_posix(), 'File is not valid JSON.'))
        elif path.suffix in {'.yaml', '.yml'}:
            try:
                yaml.safe_load(path.read_text(encoding='utf-8'))
            except yaml.YAMLError:
                findings.append(_finding('invalid-yaml', relative.as_posix(), 'File is not valid YAML.'))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        findings = check(args.root)
    except (OSError, UnicodeError) as exc:
        parser.exit(2, f'Cannot inspect skill: {type(exc).__name__}\n')
    errors = sum(item['severity'] == 'error' for item in findings)
    if args.json:
        print(json.dumps({'errors': errors, 'findings': findings}, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(f"{item['severity'].upper()} {item['path']} [{item['code']}]: {item['message']}")
        print(f'Skill check: {errors} errors, {len(findings) - errors} warnings. External links and live Telegram are not checked.')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
