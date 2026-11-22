# emailctx

Client emails go through an LLM pipeline that extracts cited requirements per client. An MCP server (`emailctx serve`) lets every Claude session read and update that state.

## Answering questions about clients

- Questions about a client (Acme, Nova, Orion, or any company) are answered from the **emailctx MCP tools**: `list_companies`, then `get_company_context`, citing the footnotes. Use them without being asked.
- Do **not** answer client questions from files in this repo. `dummy_data/` and `expected_context/` are the challenge's sample project for `emailctx eval`, not live client state. Live state is only in `emailctx.db`, served by the MCP server.
- If the emailctx tools aren't available, say so, and point the user to `/mcp` (connect and sign in) and [docs/SETUP.md](docs/SETUP.md) step 9. Don't fall back to the sample files.

## Setting it up

[docs/SETUP.md](docs/SETUP.md) is the full guide: Google Cloud (Desktop client for Gmail, Web client for MCP login, test users), `.env`, LLM provider/models, `registry.yaml`, `emailctx auth`, `emailctx serve --auth google --poll-minutes 1`, connecting Claude Code, and troubleshooting. When helping someone set up, walk them through it step by step and ask for each value they must provide.

## Secrets

- `.env` (API keys, OAuth client secret, allow-list) and `client_secret.json` are gitignored. Never print, commit or paste their contents. To check them, print only whether a value is present or has the right shape.
- The Gmail token lives in the OS keyring (`emailctx auth`), never in a file.

## Development

- Install: `python3 -m venv .venv && .venv/bin/pip install --prefer-binary -e ".[dev]"`
- Tests: `.venv/bin/pytest`. Offline, with recorded LLM answers and a fake Gmail/Google. `tests/conftest.py` sets `EMAILCTX_NO_DOTENV` so a real `.env` never reaches tests.
- Layout: `src/emailctx/cli.py` (commands), `mcp_server.py` (tools + server instructions), `oauth.py` (MCP login via Google), `sources/gmail_sync.py` (polling), `pipeline/` (gate → extract → validate → apply), `llm.py` (Anthropic/Gemini), `store/models.py` (SQLite schema).
- Design: [docs/DESIGN.md](docs/DESIGN.md).
