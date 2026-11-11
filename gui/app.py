#!/usr/bin/env python3
"""Papermorph GUI: pick a source, a model, an agent and pictures in a browser page, then start the agent.

    python3 gui/app.py                  # serves http://127.0.0.1:8770/ and opens it in the browser
    python3 gui/app.py --port 9000 --no-browser
    python3 gui/app.py --root ~/work    # the folder that holds site/ and .claude/ (default: this repository)

The page only collects choices. "Start" then, in this order:
  1. saves the PDF and the pictures under books/<book>/ (a private working folder that git ignores);
  2. writes books/<book>/START_PROMPT.md, the intake answers for the Papermorph skill, built from the form;
  3. writes a small launcher script beside it and opens that in a terminal window, where the chosen agent
     starts with a one-line first message that points at START_PROMPT.md, so it can ask you questions.

Standard library only. The server listens on 127.0.0.1 and every request needs the token it served the page with.
"""
from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WIN = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'
PY = 'python' if IS_WIN else 'python3'

KINDS = {
    'lesson': ('Lesson', 'a textbook PDF turned into animated lessons'),
    'picture': ('Picture book', 'an animated paper-puppet picture book from a story'),
    'documentary': ('Documentary', 'a narrated documentary about a book or an idea'),
    'shorts': ('Shorts', 'upright shorts for a phone feed that make one point in under three minutes'),
}
TIERS = {
    '1': '1, diagrams that change (symbols, graphs and shapes built in code; a smaller model can do most steps)',
    '2': '2, illustrated scenes (characters, camera and a storyboard; the picture step wants a strong model)',
}
SLUG = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$')
LANG = re.compile(r'^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$')
MODEL = re.compile(r'^[A-Za-z0-9._:/@+-]{1,100}$')
PIC_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}
LIMITS = {'pdf': 1 << 30, 'picture': 40 << 20, 'pictures': 60}  # bytes, bytes, count
LINUX_TERMINALS = [  # program, flags that come before the command to run
    ('x-terminal-emulator', ['-e']), ('gnome-terminal', ['--']), ('konsole', ['-e']),
    ('xfce4-terminal', ['-x']), ('kitty', []), ('alacritty', ['-e']), ('xterm', ['-e']),
]


class FormError(Exception):
    """A problem the person can fix; shown next to the field it names."""

    def __init__(self, message: str, field: str = ''):
        super().__init__(message)
        self.field = field


# ---------------------------------------------------------------- agents and models

def load_agents() -> list[dict]:
    try:
        agents = json.loads((HERE / 'agents.json').read_text(encoding='utf-8'))['agents']
        for a in agents:
            if not all(k in a for k in ('id', 'name', 'command')):
                raise KeyError('each agent needs "id", "name" and "command"')
            a.setdefault('note', '')
            a.setdefault('model_prefix', '')
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise FormError(f'gui/agents.json is not valid: {e}')
    return agents


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local servers: never through a proxy


def get_json(url: str, timeout: float = 1.5):
    with _OPENER.open(urllib.request.Request(url), timeout=timeout) as r:
        return json.load(r)


def ollama_base() -> str:
    host = os.environ.get('OLLAMA_HOST', '').strip() or '127.0.0.1:11434'
    if '://' not in host:
        host = 'http://' + host
    return host.replace('0.0.0.0', '127.0.0.1').rstrip('/')


def list_models() -> dict:
    """Ask the local model servers (not the internet) which models they have."""
    sources = (
        ('ollama', 'Ollama', ollama_base() + '/api/tags', lambda j: [m['name'] for m in j.get('models', [])]),
        ('lmstudio', 'LM Studio', 'http://127.0.0.1:1234/v1/models', lambda j: [m['id'] for m in j.get('data', [])]),
    )
    out = {}
    for key, label, url, pick in sources:
        try:
            out[key] = {'label': label, 'url': url, 'models': sorted(pick(get_json(url)))}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            out[key] = {'label': label, 'url': url, 'models': [], 'error': 'not reachable'}
    return out


def write_opencode_config(root: Path) -> dict:
    """OpenCode only knows the models an opencode.json lists. Write one for Ollama; never change an existing file."""
    path = root / 'opencode.json'
    if path.exists():
        return {'written': False, 'message': 'opencode.json already exists here, so it was left alone. '
                                             'Make sure it lists your Ollama models.'}
    models = list_models()['ollama']['models']
    if not models:
        raise FormError('Ollama has no models to list. Start Ollama and pull a model first.')
    cfg = {
        '$schema': 'https://opencode.ai/config.json',
        'provider': {'ollama': {
            'npm': '@ai-sdk/openai-compatible', 'name': 'Ollama (local)',
            'options': {'baseURL': ollama_base() + '/v1'},
            'models': {m: {'name': m} for m in models},
        }},
    }
    path.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
    return {'written': True, 'message': f'Wrote opencode.json with {len(models)} Ollama model'
                                        f'{"" if len(models) == 1 else "s"}. Pick one as ollama/<name>.'}


# ---------------------------------------------------------------- files

def safe_name(name: str, exts: set[str]) -> str | None:
    """A file name that is safe to create: no folders, plain characters, one of the allowed extensions."""
    base = Path(str(name).replace('\\', '/')).name
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in exts:
        return None
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._-')[:60] or 'picture'
    return stem + ext


def looks_like(kind: str, ext: str, head: bytes) -> bool:
    """The first bytes of a file say whether it is what its name claims."""
    if kind == 'pdf':
        return head.startswith(b'%PDF-')
    checks = {
        '.png': head.startswith(b'\x89PNG'),
        '.jpg': head[:3] == b'\xff\xd8\xff',
        '.jpeg': head[:3] == b'\xff\xd8\xff',
        '.gif': head[:4] == b'GIF8',
        '.webp': head[:4] == b'RIFF' and head[8:12] == b'WEBP',
        '.svg': b'<svg' in head.lower(),
    }
    return checks.get(ext, False)


def unique(path: Path) -> Path:
    n, p = 2, path
    while p.exists():
        p = path.with_name(f'{path.stem}-{n}{path.suffix}')
        n += 1
    return p


def existing_pictures(work: Path) -> list[str]:
    folder = work / 'pictures'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in PIC_EXT)


# ---------------------------------------------------------------- the brief and the command

def quote(value: str) -> str:
    if IS_WIN:
        return '"' + value.replace('"', "'") + '"'
    return shlex.quote(value)


def build_command(template: str, model: str, message: str, prompt_file: str) -> str:
    """Fill {model}, {message} and {prompt_file}. A [part with {model}] is left out when no model is chosen."""
    def optional(m: re.Match) -> str:
        inner = m.group(1)
        return inner if (model or '{model}' not in inner) else ''
    cmd = re.sub(r'\[([^\[\]]*)\]', optional, template)
    cmd = cmd.replace('{model}', quote(model)).replace('{message}', quote(message))
    cmd = cmd.replace('{prompt_file}', quote(prompt_file))
    return re.sub(r'\s+', ' ', cmd).strip()


def clean_text(text: str, limit: int) -> str:
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(text or ''))
    return re.sub(r'\s+', ' ', text).strip()[:limit].replace('"', "'")


def build_brief(f: dict, has_pdf: bool, pdf_name: str, pictures: list[str], exists: bool) -> str:
    slug, kind = f['slug'], f['kind']
    label, what = KINDS[kind]
    L = [
        'Read .claude/skills/papermorph/SKILL.md in this folder and follow it. "SKILL" means '
        '.claude/skills/papermorph. Work in this folder (it contains site/).',
        '',
        'The person started this from the Papermorph GUI and has already chosen the items below. '
        'Treat them as intake answers: do not ask for them again.',
        '',
        f'- Kind of book: {label.lower()} ({what})',
        f'- Book name (slug): {slug}' + (' (already in site/: resume it from its own files; do not run new_book.py)'
                                         if exists else ' (new)'),
    ]
    if has_pdf:
        name = f' (original file name: {pdf_name})' if pdf_name else ''
        L.append(f'- Source PDF: books/{slug}/book.pdf{name}; the intake step "find the PDF" is done')
    elif exists:
        L.append('- Source: the book already exists; continue from the files it was made from.')
    elif kind == 'lesson':
        L.append('- Source PDF: none yet. Ask me for it.')
    else:
        L.append('- Source: no PDF attached. Ask me for the story or the idea.')
    if f['chapter']:
        L.append(f'- Pilot chapter: {f["chapter"]} (use it for new_book.py --chapter)')
    else:
        L.append('- Pilot chapter: not chosen. Offer two or three after reading the book, as the intake says.')
    L.append(f'- Language: {f["lang"]} (use it for new_book.py --lang)')
    if kind == 'lesson':
        L.append('- Tier: ' + (TIERS[f['tier']] if f['tier'] in TIERS else
                               'not chosen. Recommend one at the intake, from the chapter we pick.'))
    if f['model']:
        L += [f'- The model you run on: {f["model"]}. If you cannot start subagents or choose their model, run every '
              'step yourself on the model that runs you and write that model in style.json `models`.']
    if pictures:
        L.append(f'- Pictures from the person ({len(pictures)}), in books/{slug}/pictures/: ' + ', '.join(pictures))
        if f['credits']:
            L.append(f'- Where the pictures come from, for the credits: "{f["credits"]}"')
        else:
            L.append('- Where the pictures come from: not stated. Ask me before using any of them in a published book.')
        L.append('  Use them where they fit; copy the ones you use into site/<book>/img/ as the skill describes '
                 'and credit them.')
    L += ['', f'On this computer the Python command is `{PY}`; use it wherever the docs say python3.']
    if f['offline']:
        L += [
            '',
            'This computer has no internet. Use nothing online: no Edge TTS, ElevenLabs, OpenRouter, OpenAI, web '
            'search, commons.py, geo.py or refs.py.',
            'Narration: set "engine": "kokoro" and the voice "af_heart" in every narration file (English only).',
            'Run Python tools as `uv run --offline --with ...`.',
        ]
    L += [
        '',
        "Go through the rest of the skill's intake with me, one question at a time, and wait for my approval after "
        'the pilot chapter, as the skill says.',
        'You must be able to view images; tell me now if you cannot.',
    ]
    return '\n'.join(L) + '\n'


def launcher_lines(root: Path, command: str, offline: bool) -> list[str]:
    if IS_WIN:
        lines = [f'cd /d "{root}"']
        if offline:
            lines += ['set UV_OFFLINE=1', 'set HF_HUB_OFFLINE=1']
    else:
        lines = [f'cd {shlex.quote(str(root))}']
        if offline:
            lines.append('export UV_OFFLINE=1 HF_HUB_OFFLINE=1')
    return lines + [command]


def evaluate(app: 'App', d: dict, strict: bool) -> dict:
    """Check the form and build the brief and command. Not strict: collect problems and fill gaps, for the live
    preview. Strict: the files must really be on disk, for Start."""
    problems: list[dict] = []

    def bad(field: str, message: str):
        problems.append({'field': field, 'message': message})

    kind = d.get('kind')
    if kind not in KINDS:
        bad('kind', 'Choose what to make.')
        kind = 'lesson'
    slug = str(d.get('slug') or '').strip()
    valid_slug = bool(SLUG.match(slug))
    if not slug:
        bad('slug', 'Give the book a name.')
    elif not valid_slug:
        bad('slug', 'Use lowercase letters, digits and hyphens in the book name, for example algebra-1.')
    shown = slug if valid_slug else 'your-book'

    chapter = ''
    if str(d.get('chapter') or '').strip():
        try:
            n = int(str(d['chapter']).strip())
            if not 1 <= n <= 999:
                raise ValueError
            chapter = str(n)
        except ValueError:
            bad('chapter', 'The chapter is a number from 1 to 999, or leave it empty.')
    lang = str(d.get('lang') or 'en').strip() or 'en'
    if not LANG.match(lang):
        bad('lang', 'Use a language code such as en, ru, zh, ja or ar.')
        lang = 'en'
    tier = str(d.get('tier') or 'auto') if kind == 'lesson' else ''
    model = str(d.get('model') or '').strip()
    if model and not MODEL.match(model):
        bad('model', 'The model name may only use letters, digits and . _ : / @ + -')
        model = ''
    agents = {a['id']: a for a in load_agents()}
    agent = agents.get(d.get('agent'))
    if agent is None:
        bad('agent', 'Choose an agent.')
        agent = next(iter(agents.values()))

    work = app.root / 'books' / shown
    exists = valid_slug and (app.root / 'site' / slug).is_dir()
    pdf_on_disk = valid_slug and (work / 'book.pdf').is_file()
    pdf_name = clean_text(d.get('pdf_name', ''), 80)
    has_pdf = pdf_on_disk or (not strict and bool(pdf_name))
    if kind == 'lesson' and not has_pdf and not exists:
        bad('pdf', 'Choose the textbook PDF for a lesson.')

    pictures = set(existing_pictures(work)) if valid_slug else set()
    pending = d.get('pictures')
    for raw in pending if isinstance(pending, list) else []:
        name = safe_name(raw, PIC_EXT)
        if name is None:
            bad('pictures', f'{clean_text(raw, 40)}: pictures must be png, jpg, gif, webp or svg files.')
        elif strict and not (work / 'pictures' / name).is_file():
            bad('pictures', f'{name} was not uploaded.')
        else:
            pictures.add(name)
    if len(pictures) > LIMITS['pictures']:
        bad('pictures', f'Add at most {LIMITS["pictures"]} pictures.')

    form = {'kind': kind, 'slug': shown, 'chapter': chapter, 'lang': lang, 'tier': tier, 'model': model,
            'offline': bool(d.get('offline')), 'credits': clean_text(d.get('credits', ''), 500)}
    brief = build_brief(form, has_pdf, pdf_name, sorted(pictures), exists)

    template = str(d.get('command') or '') if agent['id'] == 'custom' else agent['command']
    message = f'Read books/{shown}/START_PROMPT.md and follow it.'
    command = build_command(template, model, message, f'books/{shown}/START_PROMPT.md')
    if not command:
        bad('command', 'Type the command that starts your agent.')
    elif '{message}' not in template and '{prompt_file}' not in template:
        bad('command', 'The command needs {message} or {prompt_file}, or the agent never gets its instructions.')
    return {'problems': problems, 'brief': brief, 'command': command, 'agent': agent, 'form': form,
            'lines': launcher_lines(app.root, command, form['offline']), 'work': work}


# ---------------------------------------------------------------- starting the agent

def find_linux_terminal() -> list[str] | None:
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return None
    for exe, flags in LINUX_TERMINALS:
        path = shutil.which(exe)
        if path:
            return [path, *flags]
    return None


def terminal_name() -> str | None:
    if IS_WIN:
        return 'Command Prompt'
    if IS_MAC:
        return 'Terminal'
    term = find_linux_terminal()
    return Path(term[0]).name if term else None


def write_launcher(work: Path, lines: list[str]) -> Path:
    if IS_WIN:
        path = work / 'start.bat'
        body = ['@echo off', 'chcp 65001 >nul', *lines, 'echo.',
                'echo The agent has exited. Press any key to close this window.', 'pause >nul']
        path.write_text('\r\n'.join(body) + '\r\n', encoding='utf-8')
    else:
        path = work / ('start.command' if IS_MAC else 'start.sh')
        body = ['#!/usr/bin/env bash', lines[0] + ' || exit 1', *lines[1:], 'status=$?', 'echo',
                'echo "The agent has exited (status $status). Press Enter to close this window."', 'read -r _']
        path.write_text('\n'.join(body) + '\n', encoding='utf-8')
        path.chmod(0o755)
    return path


def open_terminal(script: Path, cwd: Path) -> tuple[bool, str]:
    """Open the launcher in a new terminal window. Returns (opened, why not)."""
    quiet = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    try:
        if IS_WIN:
            subprocess.Popen(['cmd.exe', '/k', str(script)], cwd=cwd,
                             creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
        elif IS_MAC:
            subprocess.Popen(['open', '-a', 'Terminal', str(script)], cwd=cwd, **quiet)
        else:
            term = find_linux_terminal()
            if not term:
                return False, 'no terminal program or desktop session was found'
            subprocess.Popen([*term, 'bash', str(script)], cwd=cwd, start_new_session=True, **quiet)
    except OSError as e:
        return False, str(e)
    return True, ''


def start(app: 'App', d: dict) -> dict:
    r = evaluate(app, d, strict=True)
    if r['problems']:
        p = r['problems'][0]
        raise FormError(p['message'], p['field'])
    work: Path = r['work']
    work.mkdir(parents=True, exist_ok=True)
    (work / 'START_PROMPT.md').write_text(r['brief'], encoding='utf-8')
    script = write_launcher(work, r['lines'])
    opened, why = open_terminal(script, app.root)
    rel = f'books/{r["form"]["slug"]}'
    name = 'Your agent' if r['agent']['id'] == 'custom' else r['agent']['name']
    return {'opened': opened, 'why': why, 'agent': name, 'prompt_file': f'{rel}/START_PROMPT.md',
            'launcher': f'{rel}/{script.name}', 'manual': '\n'.join(r['lines'])}


# ---------------------------------------------------------------- the server

class App:
    def __init__(self, root: Path):
        self.root = root
        self.token = secrets.token_urlsafe(18)
        self.page = (HERE / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', self.token)

    def state(self) -> dict:
        site = self.root / 'site'
        return {
            'root': str(self.root), 'platform': sys.platform, 'python': PY, 'agents': load_agents(),
            'books': sorted(p.name for p in site.iterdir() if p.is_dir()) if site.is_dir() else [],
            'skill': (self.root / '.claude' / 'skills' / 'papermorph' / 'SKILL.md').is_file(),
            'terminal': terminal_name(),
            'kinds': {k: {'label': v[0], 'what': v[1]} for k, v in KINDS.items()},
            'limits': {'pdf': LIMITS['pdf'], 'picture': LIMITS['picture'], 'pictures': LIMITS['pictures']},
        }

    def upload(self, query: dict, rfile, length: int) -> dict:
        kind = query.get('kind', [''])[0]
        slug = query.get('slug', [''])[0]
        name = query.get('name', [''])[0]
        if kind not in ('pdf', 'picture'):
            raise FormError('Unknown kind of upload.')
        if not SLUG.match(slug):
            raise FormError('Give the book a valid name before choosing files.', 'slug')
        limit = LIMITS[kind]
        if length < 0:
            raise FormError('The upload had no size.')
        if length > limit:
            raise FormError(f'{clean_text(name, 40)} is larger than {limit >> 20} MB.', kind)
        work = self.root / 'books' / slug
        if kind == 'pdf':
            dest, ext = work / 'book.pdf', '.pdf'
        else:
            safe = safe_name(name, PIC_EXT)
            if safe is None:
                raise FormError(f'{clean_text(name, 40)}: pictures must be png, jpg, gif, webp or svg files.', 'pictures')
            dest, ext = work / 'pictures' / safe, Path(safe).suffix
            # Same name and same size: the same picture again, so replace it; otherwise keep both.
            if dest.exists() and dest.stat().st_size != length:
                dest = unique(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + '.part')
        head = b''
        try:
            with open(tmp, 'wb') as out:
                left = length
                while left:
                    chunk = rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise FormError('The upload was cut off. Try again.')
                    if len(head) < 2048:
                        head += chunk[:2048 - len(head)]
                    out.write(chunk)
                    left -= len(chunk)
            if not looks_like(kind, ext.lower(), head):
                raise FormError(f'{clean_text(name, 40)} does not look like a real {ext.lstrip(".")} file.', kind)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {'name': dest.name, 'size': length}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'PapermorphGUI/1'

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, *args):  # keep the terminal quiet
        pass

    def allowed(self, api: bool) -> bool:
        """Only this machine's own page may use the server: right Host, right Origin, and the token for the API.
        This stops other websites in the browser from starting programs through it."""
        port = self.server.server_address[1]
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host', '') not in hosts:
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{h}' for h in hosts}:
            return False
        if api and not hmac.compare_digest(self.headers.get('X-Token', '').encode(), self.app.token.encode()):
            return False
        return True

    def reply(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if ctype.startswith('text/html'):
            self.send_header('Content-Security-Policy', "default-src 'none'; img-src blob: data:; "
                             "style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, code: int, obj):
        self.reply(code, json.dumps(obj).encode('utf-8'), 'application/json; charset=utf-8')

    def api(self, fn):
        try:
            result = fn()
        except FormError as e:
            self.reply_json(400, {'error': str(e), 'field': e.field})
        except Exception as e:  # a bug: say so in the page and in the terminal
            traceback.print_exc()
            self.reply_json(500, {'error': f'{type(e).__name__}: {e}'})
        else:
            self.reply_json(200, result)

    def read_json(self) -> dict:
        length = int(self.headers.get('Content-Length') or 0)
        if not 0 <= length <= 1 << 20:
            raise FormError('The request is too large.')
        try:
            data = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            raise FormError('The request was not valid JSON.')
        if not isinstance(data, dict):
            raise FormError('The request was not a JSON object.')
        return data

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/':
            if not self.allowed(False):
                return self.reply(403, b'Forbidden', 'text/plain')
            return self.reply(200, self.app.page.encode('utf-8'), 'text/html; charset=utf-8')
        if not self.allowed(True):
            return self.reply(403, b'Forbidden', 'text/plain')
        if path == '/api/state':
            return self.api(self.app.state)
        if path == '/api/models':
            return self.api(list_models)
        self.reply(404, b'Not found', 'text/plain')

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if not self.allowed(True):
            return self.reply(403, b'Forbidden', 'text/plain')
        app = self.app
        if url.path == '/api/upload':
            length = int(self.headers.get('Content-Length') or -1)
            return self.api(lambda: app.upload(urllib.parse.parse_qs(url.query), self.rfile, length))
        if url.path == '/api/preview':
            def preview():
                r = evaluate(app, self.read_json(), strict=False)
                return {'problems': r['problems'], 'brief': r['brief'], 'manual': '\n'.join(r['lines'])}
            return self.api(preview)
        if url.path == '/api/start':
            return self.api(lambda: start(app, self.read_json()))
        if url.path == '/api/opencode-config':
            return self.api(lambda: write_opencode_config(app.root))
        self.reply(404, b'Not found', 'text/plain')


def make_server(root: Path, port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = App(root)  # type: ignore[attr-defined]
    return server


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8770, help='port to listen on (default 8770)')
    ap.add_argument('--root', default=str(HERE.parent), help='folder holding site/ and .claude/ (default: the repository)')
    ap.add_argument('--no-browser', action='store_true', help='do not open the page in the browser')
    a = ap.parse_args()
    root = Path(a.root).expanduser().resolve()
    if not (root / '.claude' / 'skills' / 'papermorph' / 'SKILL.md').is_file():
        print(f'Warning: {root} has no .claude/skills/papermorph/SKILL.md. Use --root with the Papermorph folder.')
    try:
        server = make_server(root, a.port)
    except OSError as e:
        sys.exit(f'Cannot listen on port {a.port} ({e}). Try --port {a.port + 1}.')
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    print(f'Papermorph GUI: {url}\nWorking in {root}\nPress Ctrl+C to stop.')
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
