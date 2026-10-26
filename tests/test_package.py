"""Consumer-visible boundaries of the portable distribution, not source snapshots."""
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'package_skill.py'
spec = importlib.util.spec_from_file_location('release_package', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'telegram-engineering'
        self.root.mkdir()
        (self.root / 'SKILL.md').write_text('---\nname: telegram-engineering\ndescription: Use when working on Telegram.\n---\n# Skill\n', encoding='utf-8')
        (self.root / 'references').mkdir()
        (self.root / 'references' / 'guide.md').write_text('Пример', encoding='utf-8')

    def full_checkout(self):
        for relative in ('tests/test_example.py', 'evals/evals.json', '.github/workflows/check.yml'):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}', encoding='utf-8')

    def test_release_excludes_local_secrets_and_runtime_state(self):
        for rel in ('PROJECT_STATE.md', '.env', 'examples/bot/.env', 'examples/bot/data/users.sqlite', 'examples/bot/cache/session.session', 'examples/bot/users.sqlite-journal', 'examples/bot/account.session-journal', 'examples/bot/__pycache__/module.pyc', '.git/config'):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('private local state', encoding='utf-8')
        example = self.root / 'examples' / 'bot' / '.env.example'
        example.write_text('BOT_TOKEN=\n', encoding='utf-8')
        archive = module.package(self.root, Path(self.temp.name) / 'output', kind='skill')
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            self.assertIn('telegram-engineering/SKILL.md', names)
            self.assertIn('telegram-engineering/examples/bot/.env.example', names)
            self.assertNotIn('telegram-engineering/.env', names)
            self.assertNotIn('telegram-engineering/PROJECT_STATE.md', names)
            self.assertFalse(any('private local state' in bundle.read(n).decode('utf-8') for n in names))

    def test_source_and_skill_distributions_have_distinct_scopes(self):
        for rel in ('tests/test_behavior.py', '.github/workflows/check.yml', 'evals/evals.json', 'README.md'):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}', encoding='utf-8')
        output = Path(self.temp.name) / 'out'
        source = module.package(self.root, output, kind='source')
        skill = module.package(self.root, output, kind='skill')
        with zipfile.ZipFile(source) as bundle:
            self.assertIn('telegram-engineering/tests/test_behavior.py', bundle.namelist())
        with zipfile.ZipFile(skill) as bundle:
            self.assertNotIn('telegram-engineering/tests/test_behavior.py', bundle.namelist())
            self.assertNotIn('telegram-engineering/evals/evals.json', bundle.namelist())
            self.assertIn('telegram-engineering/README.md', bundle.namelist())

    def test_packages_are_reproducible_and_unicode_is_preserved(self):
        a = module.package(self.root, Path(self.temp.name) / 'a', kind='skill')
        b = module.package(self.root, Path(self.temp.name) / 'b', kind='skill')
        self.assertEqual(a.read_bytes(), b.read_bytes())
        with zipfile.ZipFile(a) as bundle:
            self.assertEqual(bundle.read('telegram-engineering/references/guide.md').decode('utf-8'), 'Пример')

    def test_missing_skill_is_rejected_without_output(self):
        (self.root / 'SKILL.md').unlink()
        with self.assertRaises(ValueError):
            module.package(self.root, Path(self.temp.name) / 'out', kind='skill')

    def test_output_inside_source_does_not_recursively_include_archives(self):
        output = self.root / 'dist'
        self.full_checkout()
        first = module.package(self.root, output, kind='source')
        original_bytes = first.read_bytes()
        second = module.package(self.root, output, kind='source')
        self.assertEqual(original_bytes, second.read_bytes())
        with zipfile.ZipFile(second) as bundle:
            self.assertFalse(any('/dist/' in n for n in bundle.namelist()))

    def test_custom_output_subdirectory_is_not_distributed(self):
        output = self.root / 'release-output'
        self.full_checkout()
        output.mkdir()
        (output / 'private-note.txt').write_text('not source', encoding='utf-8')
        archive = module.package(self.root, output, kind='source')
        with zipfile.ZipFile(archive) as bundle:
            self.assertFalse(any('/release-output/' in name for name in bundle.namelist()))

    def test_output_equal_to_source_root_is_rejected(self):
        with self.assertRaises(ValueError):
            module.package(self.root, self.root, kind='source')

    def test_output_ancestor_is_rejected(self):
        with self.assertRaises(ValueError):
            module.package(self.root, self.root.parent, kind='skill')

    def test_token_in_selected_file_blocks_packaging_without_echo(self):
        token = '123456789:' + 'jK7_' * 9
        template = self.root / 'examples' / 'bot' / '.env.example'
        template.parent.mkdir(parents=True)
        template.write_text('BOT_TOKEN=' + token, encoding='utf-8')
        with self.assertRaises(ValueError) as error:
            module.package(self.root, Path(self.temp.name) / 'out', kind='skill')
        self.assertNotIn(token, str(error.exception))

    def test_source_mode_requires_full_checkout(self):
        with self.assertRaises(ValueError):
            module.package(self.root, Path(self.temp.name) / 'out', kind='source')

    def test_renamed_checkout_is_rejected_with_name_hint(self):
        renamed = self.root.with_name('telegram-engineering-main')
        self.root.rename(renamed)
        with self.assertRaises(ValueError) as error:
            module.package(renamed, Path(self.temp.name) / 'out', kind='skill')
        self.assertIn('telegram-engineering', str(error.exception))

    def test_windows_junction_cannot_include_outside_files(self):
        import os
        import subprocess
        if os.name != 'nt':
            self.skipTest('Windows junction behavior')
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'private.txt').write_text('outside-root-secret', encoding='utf-8')
        junction = self.root / 'references' / 'outside'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], capture_output=True)
        if result.returncode:
            self.skipTest('Cannot create local junction in this environment')
        try:
            with self.assertRaises(ValueError):
                module.package(self.root, Path(self.temp.name) / 'out', kind='skill')
        finally:
            os.rmdir(junction)


if __name__ == '__main__':
    unittest.main()
