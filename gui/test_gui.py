"""Tests for the Papermorph GUI server. Run from the repository folder:

    python3 -m unittest discover -s gui -v

They start the server on a free port with a temporary folder as its root, so nothing in the repository changes.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402

PDF = b'%PDF-1.4\n%%EOF\n'
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 20


class BuildCommand(unittest.TestCase):
    def test_model_part_is_left_out_without_a_model(self):
        t = 'agent [--model {model}] --prompt {message}'
        self.assertEqual(app.build_command(t, '', 'hi there', 'f.md'), "agent --prompt 'hi there'" if not app.IS_WIN
                         else 'agent --prompt "hi there"')

    def test_model_part_is_kept_with_a_model(self):
        cmd = app.build_command('agent [--model {model}] {prompt_file}', 'gemma4:26b', 'm', 'books/x/START_PROMPT.md')
        self.assertIn('--model', cmd)
        self.assertIn('gemma4:26b', cmd)

    def test_a_message_cannot_break_out_of_its_quotes(self):
        cmd = app.build_command('agent {message}', '', "x'; rm -rf ~ #", 'f')
        if not app.IS_WIN:
            self.assertEqual(__import__('shlex').split(cmd), ['agent', "x'; rm -rf ~ #"])


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / 'site' / 'old-book').mkdir(parents=True)
        skill = cls.root / '.claude' / 'skills' / 'papermorph'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('skill')
        cls.server = app.make_server(cls.root, 0)
        cls.port = cls.server.server_address[1]
        cls.token = cls.server.app.token
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=True, host=None, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        headers = {'Host': host or f'127.0.0.1:{self.port}'}
        if token:
            headers['X-Token'] = self.token
        if origin:
            headers['Origin'] = origin
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def upload(self, kind, name, data, slug='demo'):
        return self.call('POST', f'/api/upload?kind={kind}&slug={slug}&name={name}', data)

    # --- who may talk to the server
    def test_page_is_served_to_this_computer_only(self):
        self.assertEqual(self.call('GET', '/', token=False)[0], 200)
        self.assertEqual(self.call('GET', '/', token=False, host='evil.example')[0], 403)

    def test_api_needs_the_token(self):
        self.assertEqual(self.call('GET', '/api/state', token=False)[0], 403)
        self.assertEqual(self.call('GET', '/api/state')[0], 200)

    def test_a_foreign_origin_is_refused_even_with_the_token(self):
        self.assertEqual(self.call('POST', '/api/preview', {}, origin='http://evil.example')[0], 403)

    # --- the brief
    def test_preview_lists_what_is_missing(self):
        status, r = self.call('POST', '/api/preview', {'kind': 'lesson', 'agent': 'opencode'})
        self.assertEqual(status, 200)
        self.assertEqual({p['field'] for p in r['problems']}, {'slug', 'pdf'})

    def test_preview_builds_the_brief_and_offline_rules(self):
        form = {'kind': 'lesson', 'slug': 'algebra', 'pdf_name': 'Algebra.pdf', 'chapter': '3', 'tier': '1',
                'agent': 'opencode', 'model': 'ollama/gemma4:26b', 'offline': True, 'lang': 'en'}
        _, r = self.call('POST', '/api/preview', form)
        self.assertEqual(r['problems'], [])
        for text in ('books/algebra/book.pdf', 'Pilot chapter: 3', 'Tier: 1,', 'ollama/gemma4:26b', 'kokoro',
                     'uv run --offline'):
            self.assertIn(text, r['brief'])
        self.assertIn('UV_OFFLINE', r['manual'])

    def test_an_existing_book_is_resumed_not_recreated(self):
        _, r = self.call('POST', '/api/preview', {'kind': 'lesson', 'slug': 'old-book', 'agent': 'opencode'})
        self.assertEqual(r['problems'], [])
        self.assertIn('resume it', r['brief'])

    def test_a_custom_command_must_receive_the_message(self):
        _, r = self.call('POST', '/api/preview', {'kind': 'shorts', 'slug': 's', 'agent': 'custom', 'command': 'my-agent'})
        self.assertEqual([p['field'] for p in r['problems']], ['command'])

    def test_a_bad_book_name_is_a_problem(self):
        _, r = self.call('POST', '/api/preview', {'kind': 'shorts', 'slug': '../x', 'agent': 'opencode'})
        self.assertIn('slug', [p['field'] for p in r['problems']])

    # --- uploads
    def test_pdf_and_pictures_are_saved_under_books(self):
        self.assertEqual(self.upload('pdf', 'Book.pdf', PDF)[0], 200)
        self.assertEqual(self.upload('picture', 'river.png', PNG)[1]['name'], 'river.png')
        self.assertTrue((self.root / 'books' / 'demo' / 'book.pdf').is_file())
        self.assertTrue((self.root / 'books' / 'demo' / 'pictures' / 'river.png').is_file())

    def test_a_file_that_is_not_what_its_name_says_is_refused(self):
        self.assertEqual(self.upload('pdf', 'x.pdf', b'hello')[0], 400)
        self.assertEqual(self.upload('picture', 'x.png', b'not a png')[0], 400)
        self.assertEqual(self.upload('picture', 'run.exe', PNG)[0], 400)

    def test_folders_in_a_file_name_are_dropped(self):
        status, r = self.upload('picture', '../../evil.png', PNG)
        self.assertEqual((status, r['name']), (200, 'evil.png'))
        self.assertFalse((self.root.parent / 'evil.png').exists())

    def test_same_name_and_size_replaces_and_a_different_size_is_kept(self):
        self.upload('picture', 'same.png', PNG)
        self.assertEqual(self.upload('picture', 'same.png', PNG)[1]['name'], 'same.png')
        self.assertEqual(self.upload('picture', 'same.png', PNG + b'1')[1]['name'], 'same-2.png')

    def test_upload_needs_a_valid_book_name(self):
        self.assertEqual(self.upload('pdf', 'x.pdf', PDF, slug='..')[0], 400)

    # --- starting
    def test_start_writes_the_brief_and_a_launcher_and_opens_a_terminal(self):
        self.upload('pdf', 'Book.pdf', PDF, slug='go')
        form = {'kind': 'lesson', 'slug': 'go', 'agent': 'opencode', 'model': 'ollama/x', 'pictures': []}
        with mock.patch.object(app, 'open_terminal', return_value=(True, '')) as opened:
            status, r = self.call('POST', '/api/start', form)
        self.assertEqual(status, 200)
        self.assertTrue(r['opened'])
        work = self.root / 'books' / 'go'
        self.assertIn('books/go/book.pdf', (work / 'START_PROMPT.md').read_text())
        launcher = work / Path(r['launcher']).name
        self.assertIn('--prompt', launcher.read_text())
        opened.assert_called_once()
        if not app.IS_WIN:
            self.assertTrue(launcher.stat().st_mode & 0o100)

    def test_start_without_a_terminal_still_returns_the_lines_to_paste(self):
        self.upload('pdf', 'Book.pdf', PDF, slug='noterm')
        form = {'kind': 'lesson', 'slug': 'noterm', 'agent': 'qwen'}
        with mock.patch.object(app, 'open_terminal', return_value=(False, 'no terminal')):
            _, r = self.call('POST', '/api/start', form)
        self.assertFalse(r['opened'])
        self.assertIn('qwen', r['manual'])

    def test_start_refuses_a_lesson_without_its_pdf(self):
        status, r = self.call('POST', '/api/start', {'kind': 'lesson', 'slug': 'nopdf', 'agent': 'opencode'})
        self.assertEqual((status, r['field']), (400, 'pdf'))

    def test_start_refuses_a_picture_that_was_not_uploaded(self):
        form = {'kind': 'picture', 'slug': 'pics', 'agent': 'opencode', 'pictures': ['ghost.png']}
        status, r = self.call('POST', '/api/start', form)
        self.assertEqual((status, r['field']), (400, 'pictures'))


if __name__ == '__main__':
    unittest.main()
