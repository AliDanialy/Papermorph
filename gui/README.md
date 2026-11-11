# Papermorph GUI

A small page in your browser for starting a book: choose the kind of book, the PDF, the model, the agent and your own pictures, then press Start. The agent opens in a terminal window with your choices as its first message and carries on from there, asking you the rest of the skill's questions.

The GUI does not make books itself. Making them is the agent's work, following `.claude/skills/papermorph/SKILL.md`. The GUI collects the intake answers so you do not have to type them into the agent's chat.

## Run it

From the repository folder:

```
python3 gui/app.py
```

(`python` on Windows.) It prints `http://127.0.0.1:8770/` and opens it in your browser. It needs only Python 3 and nothing else. Options: `--port 9000`, `--no-browser`, `--root <folder>` for a Papermorph folder other than the one the file is in.

## What you choose

| Field | What it does |
|---|---|
| What to make | Lesson, picture book, documentary or shorts: the four kinds the skill makes. |
| Source PDF | Saved as `books/<name>/book.pdf`. Required for a new lesson, optional for the others. |
| Book name | The folder name `site/<name>/`. Filled in from the PDF's file name. If `site/<name>/` already exists, the agent is told to resume that book. |
| First chapter, language | Optional chapter number, and the language code (default `en`). They are the answers for `new_book.py --chapter` and `--lang`. |
| Animation level | Lessons only: tier 1 (diagrams that change), tier 2 (illustrated scenes), or let the agent suggest one. |
| Agent | The program that does the work. The presets are in `gui/agents.json`. |
| Model | Free text, with suggestions from Ollama and LM Studio when they are running on this computer. |
| No internet | Adds the offline rules to the brief and sets `UV_OFFLINE=1` and `HF_HUB_OFFLINE=1` for the agent. |
| Pictures | Your own png, jpg, gif, webp or svg files, and where they come from for the credits. Saved in `books/<name>/pictures/`. |

The sheet on the right is the brief the agent will receive. It updates as you type.

## What Start does

1. Saves the PDF and pictures under `books/<name>/`. `books/` is private working space that git ignores, so nothing from it is committed.
2. Writes `books/<name>/START_PROMPT.md`, the brief shown on the sheet.
3. Writes a launcher script beside it (`start.sh`, `start.command` on macOS, `start.bat` on Windows) and opens it in a terminal window. The script changes to the repository folder and runs the agent's command with a one-line first message: "Read books/<name>/START_PROMPT.md and follow it."

If no terminal can be opened, the page shows the same lines to paste into a terminal yourself.

The first message is short on purpose. A long brief on the command line breaks on quoting rules that differ between shells, and a file does not.

## Agents

`gui/agents.json` lists the agents in the dropdown. Each has a command template:

```json
{"id": "opencode", "name": "OpenCode",
 "command": "opencode [--model {model}] --prompt {message}",
 "model_prefix": "ollama/", "note": "shown under the dropdown"}
```

- `{message}` is the one-line first message, `{prompt_file}` is the path of the brief, and `{model}` is the model field.
- `[ ... {model} ... ]` is left out when no model is chosen.
- `model_prefix` is put in front of Ollama model names in the suggestions (OpenCode names models `provider/model`).
- The `custom` entry lets you type your own command in the page. To add an agent for good, add an entry to the file.

The presets use options taken from each agent's command-line documentation: OpenCode (`--model`, `--prompt`), Qwen Code (`--model`, `-i`) and Claude Code (`--model`, a message as the argument). They have not been run against the real programs. If an option is wrong for your version, edit `agents.json`.

## Local models

The page asks Ollama (`127.0.0.1:11434`, or `OLLAMA_HOST`) and LM Studio (`127.0.0.1:1234`) which models they have. It never contacts the internet.

- **OpenCode** only knows models listed in an `opencode.json`. The "Create opencode.json" button writes one for your Ollama models into the repository folder. It never changes a file that exists. It is not ignored by git, so do not commit it.
- **Ollama's default context is small** (4,000 tokens on graphics cards under 24 GB). Start Ollama with `OLLAMA_CONTEXT_LENGTH=64000`, or the agent loses most of the skill's instructions.

## Safety

The server listens on `127.0.0.1` only. Every request must carry a random token that exists only in the page it served, and must name this computer as its host and origin. This stops other websites open in your browser from starting programs through it. Uploads are limited in size, checked against the file's first bytes, and saved under a plain file name inside `books/<name>/`.

The agent itself runs with your user's rights, like any agent started in a terminal.

## Tests

```
python3 -m unittest discover -s gui -v
```

Run from the repository folder. They start the server on a free port with a temporary folder and cover the access rules, the brief, the uploads and Start. They replace the terminal window with a stand-in, so nothing opens.

## What has and has not been tested

Tested on Linux, in a browser (light, dark and phone width), with a stand-in agent and a stand-in terminal program. The Windows and macOS code that opens a terminal has not been run on those systems. The agent presets have not been run against the real agents.
