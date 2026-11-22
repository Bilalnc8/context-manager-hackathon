# emailctx

Any coding agent, in any new session, knows where a client project stands: what the client asked for, what is done, open and verified, and what the client has already answered.

Client emails and meeting notes go through a pipeline that pulls out requirements, each cited to its source, without duplicating or inventing anything. The team tracks delivery status and client questions on top of that. Every Claude session (Claude Code, Claude Desktop, claude.ai) reaches it through one MCP server.

- **Setting it up (start here): [docs/SETUP.md](docs/SETUP.md)**: Google Cloud, Gmail, `.env`, LLM key, MCP login, connecting Claude, troubleshooting
- Challenge brief: [docs/challenge-8.md](docs/challenge-8.md)
- System design: [docs/DESIGN.md](docs/DESIGN.md)
- Google setup, for inbox reading (`emailctx auth`) and MCP server login: [Google auth: setup and handoff](docs/DESIGN.md#google-auth-setup-and-handoff)

## Setup

Requires Python 3.11+.

```powershell
py -3.13 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\pytest                      # offline: recorded LLM responses, fake Google
$env:GEMINI_API_KEY = "..."              # needed to process real client input (provider: gemini)
# or $env:ANTHROPIC_API_KEY = "sk-ant-..." with provider: anthropic in config.yaml
```

Everything lives in one SQLite database (`emailctx.db`), which the MCP server reads directly. Each company's file is also written to `companies/<company>/context.md`. Google Drive output is off by default (`output.drive.enabled` in `config.yaml`).

## Run the MCP server

```powershell
.venv\Scripts\emailctx registry load registry.yaml   # your clients (see src/emailctx/registry.py)
.venv\Scripts\emailctx serve                          # http://127.0.0.1:8000/mcp
.venv\Scripts\emailctx serve --poll-minutes 5          # also pull new client mail from Gmail
```

Connect Claude Code:

```powershell
claude mcp add --transport http emailctx http://localhost:8000/mcp
```

Then, in any session:

- `/mcp__emailctx__company Acme` loads the project and summarizes what's open.
- "Mark REQ-003 done, PR #12." calls `set_item_status`.
- "We need to ask Acme which SSO provider they use." calls `list_questions` to check, then `log_question`.
- Paste a client email or meeting notes and Claude calls `add_client_input`.
- "What exactly did the client say about REQ-004?" calls `get_source` with the footnote number.

The server is the only writer, so many people can use it at once. Writes take turns, and each company's file always shows the latest state. For Gmail polling, run `emailctx auth` once on the server machine, signed in as the shared inbox that receives client mail.

To make users sign in with Google first, run `emailctx serve --auth google` (setup: [MCP server user login](docs/DESIGN.md#2-mcp-server-user-login-built)). Every write then records the user's email.

Behind a tunnel (for Claude Desktop or a teammate), run `emailctx serve --allowed-host my-team.ngrok.app`.

## Other commands

| Command | What it does |
|---|---|
| `emailctx ingest-file mail.eml -c acme` | Process one saved email |
| `emailctx auth`, `emailctx sync` | Read client mail from Gmail (read-only) |
| `emailctx review list / assign / dismiss` | Emails the router couldn't place |
| `emailctx conflicts list / confirm` | Contradicting emails, under "Needs confirmation" |
| `emailctx render -c acme` | Re-render a company's file |
| `emailctx load-sample` | Load the sample project (`dummy_data/`, on the `sample-data` branch) into the store, in date order |
| `emailctx eval run` | Run the sample through a fresh store and score each file against `eval/facts/` (report: `eval/run/report.md`; calls the API). Needs the `sample-data` branch |
| `emailctx eval draft-facts expected.md -p acme` | Draft a facts checklist from an expected-outcome file |

Each company's file shows deliverables with their status (open, in progress, done, verified) and where the project stands: key dates and their approval, open concerns and blockers, next actions with owners, decisions, out-of-scope work, questions for the client and their answers. Every sentence of an incoming email is either captured or listed as skipped with a reason; anything else is reported as not captured.
