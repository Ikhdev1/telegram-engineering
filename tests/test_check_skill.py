"""Check reader-facing skill metadata, links and executable document samples."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check_skill.py'
spec = importlib.util.spec_from_file_location('check_skill_module', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class SkillCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'telegram-engineering'
        self.root.mkdir()
        self.skill = self.root / 'SKILL.md'
        self.skill.write_text('---\nname: telegram-engineering\ndescription: Use when creating Telegram bots.\n---\n# Telegram\n', encoding='utf-8')

    def errors(self):
        return [item for item in checker.check(self.root) if item['severity'] == 'error']

    def test_broken_relative_link_reports_repairable_path(self):
        with self.skill.open('a', encoding='utf-8') as stream:
            stream.write('\n[Guide](references/missing.md)\n')
        findings = self.errors()
        self.assertTrue(any(item['code'] == 'broken-link' and 'missing.md' in item['message'] for item in findings))

    def test_russian_link_target_and_json_remain_valid(self):
        (self.root / 'справка.md').write_text('# Справка\n', encoding='utf-8')
        with self.skill.open('a', encoding='utf-8') as stream:
            stream.write('\n[Помощь](справка.md)\n```json\n{"text":"Привет"}\n```\n')
        self.assertEqual(self.errors(), [])

    def test_malformed_document_json_is_an_error(self):
        with self.skill.open('a', encoding='utf-8') as stream:
            stream.write('\n```json\n{"text":}\n```\n')
        self.assertTrue(any(item['code'] == 'invalid-json-example' for item in self.errors()))

    def test_yaml_name_mismatch_is_not_discoverable(self):
        self.skill.write_text('---\nname: wrong-name\ndescription: Use when creating bots.\n---\n# Bot\n', encoding='utf-8')
        self.assertTrue(any(item['code'] == 'skill-name' for item in self.errors()))

    def test_local_project_state_is_not_public_documentation(self):
        (self.root / 'PROJECT_STATE.md').write_text('C:/Users/example/private', encoding='utf-8')
        self.assertEqual(self.errors(), [])

    def test_public_machine_specific_path_is_reported(self):
        with self.skill.open('a', encoding='utf-8') as stream:
            stream.write('\nUse C:/Users/alice/secret-project/run.py\n')
        self.assertTrue(any(item['code'] == 'machine-path' for item in self.errors()))

    def test_frontmatter_colon_error_is_explained(self):
        self.skill.write_text('---\nname: telegram-engineering\ndescription: Use when: bots\n---\n', encoding='utf-8')
        self.assertTrue(any(item['code'] == 'frontmatter' for item in self.errors()))

    def test_malformed_workflow_yaml_is_reported(self):
        workflow = self.root / '.github' / 'workflows' / 'check.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('jobs: [unterminated', encoding='utf-8')
        self.assertTrue(any(item['code'] == 'invalid-yaml' for item in self.errors()))

    def test_published_bot_token_is_reported_without_echo(self):
        token = '123456789:' + 'aB2_' * 9
        (self.root / 'config.py').write_text('TOKEN = ' + repr(token), encoding='utf-8')
        findings = self.errors()
        self.assertTrue(any(item['code'] == 'possible-secret' for item in findings))
        self.assertFalse(any(token in item['message'] for item in findings))

    def test_token_inside_canonical_api_url_is_reported(self):
        token = '123456789:' + 'xY3_' * 9
        (self.root / 'guide.md').write_text('https://api.telegram.org/bot' + token + '/sendMessage', encoding='utf-8')
        findings = self.errors()
        self.assertTrue(any(item['code'] == 'possible-secret' for item in findings))
        self.assertFalse(any(token in item['message'] for item in findings))

    def test_filled_environment_template_is_reported(self):
        token = '123456789:' + 'qW4_' * 9
        (self.root / '.env.example').write_text('BOT_TOKEN=' + token, encoding='utf-8')
        self.assertTrue(any(item['code'] == 'possible-secret' for item in self.errors()))


if __name__ == '__main__':
    unittest.main()
