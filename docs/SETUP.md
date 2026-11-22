# Setup: from a fresh clone to Claude answering client questions

Follow this guide top to bottom. It covers everything we hit setting this up for the first time: Google Cloud, the Gmail inbox, the LLM key, the MCP server login, and connecting Claude Code. Every step ends with a check, so you know it worked before moving on.

If you are a Claude session helping someone set this up, work through the steps in order. Ask the user for each value marked **(you provide)**, and never print the contents of `.env` or `client_secret.json`.

## How it fits together

```
Client email ──► Gmail inbox ──► emailctx serve (polls every N min)
                                   │  routes the email to a client (registry.yaml)
                                   │  extracts requirements with an LLM (Gemini or Claude)
                                   ▼
                                emailctx.db (SQLite)
                                   ▲
Claude Code / Desktop ──MCP──► emailctx serve /mcp  (Google sign-in, allow-listed emails)
```

There are two roles. Only one person (or machine) needs to do the first.

| | **Server host** (one machine) | **Teammate** (anyone using Claude) |
|---|---|---|
| Clones the repo, installs Python deps | yes | only to work on the code |
| Google Cloud project, `client_secret.json`, `.env` | yes | no |
| Signs in the Gmail inbox (`emailctx auth`) | yes | no |
| Runs `emailctx serve` | yes | no |
| Connects Claude Code and signs in with Google | yes | **yes: [step 9](#9-connect-claude-code) is all they need** |

---

## 1. Prerequisites

- Python **3.11+** (`python3 --version`)
- git
- [Claude Code](https://claude.com/claude-code) (`claude --version`)
- A Google account that will own the Google Cloud project (any account works)
- A Gmail inbox that receives the client mail (the "shared inbox")
- A Gemini API key ([Google AI Studio](https://aistudio.google.com/apikey)) **or** an Anthropic API key ([console.anthropic.com](https://console.anthropic.com))

## 2. Clone and install

macOS / Linux:

```bash
git clone https://github.com/Bilalnc8/context-manager-hackathon.git
cd context-manager-hackathon
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install --prefer-binary -e ".[dev]"
.venv/bin/pytest
```

Windows (PowerShell):

```powershell
git clone https://github.com/Bilalnc8/context-manager-hackathon.git
cd context-manager-hackathon
py -3.13 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\pytest
```

**Check:** pytest ends with `N passed`. The tests run offline, with recorded LLM answers and a fake Google.

> **Intel Mac (macOS 13) only:** if the install fails with `Failed building wheel for cryptography`, run `.venv/bin/pip install --only-binary=cryptography,cffi cryptography`, then run the install command again. The newest `cryptography` has no prebuilt package for that platform and tries to compile Rust.

In the rest of this guide, `emailctx` means `.venv/bin/emailctx` (macOS/Linux) or `.venv\Scripts\emailctx` (Windows).

## 3. Google Cloud project (server host, once)

One project holds two OAuth clients: one for reading the inbox, one for teammates signing in to the MCP server.

1. Go to [console.cloud.google.com](https://console.cloud.google.com). Open the project dropdown, click **New Project**, name it `emailctx`, and click **Create**. Make sure it is selected in the dropdown.
2. Go to **APIs & Services → Library**, find **Gmail API**, and click **Enable**.
3. Go to **APIs & Services → OAuth consent screen** (Google Auth Platform) and click **Get started**:
   - App name `emailctx`, and your support email.
   - Audience: **External**. Choose **Internal** only if the inbox and every teammate are on the same Google Workspace the project lives in.
   - Contact email, agree, **Create**.
4. Open **Audience → Test users → + Add users**. Add **the inbox address and every teammate's Google address**. While the app is in Testing, Google blocks everyone not on this list with "Access blocked".

### 3a. Desktop client (reads the inbox)

1. Go to **Clients → + Create client** and choose application type **Desktop app**, named `emailctx-desktop`. Click **Create**.
2. Click **Download JSON**. Save the file in the repo root as **`client_secret.json`** (rename it from `client_secret_<id>.json`). It's gitignored.

**Check:** `.venv/bin/python -c "import json;print(list(json.load(open('client_secret.json'))))"` prints `['installed']`. If it prints `['web']`, you downloaded the wrong client.

### 3b. Web application client (teammates sign in to the MCP server)

1. Go to **Clients → + Create client** and choose application type **Web application**, named `emailctx-mcp-login`.
2. Leave **Authorized JavaScript origins** empty.
3. Under **Authorized redirect URIs**, add exactly `http://localhost:8000/oauth/google/callback`. Add the tunnel URL too if you use one ([step 10](#10-teammates-on-other-machines)).
4. Click **Create** and copy the **Client ID** and **Client secret** into `.env` ([step 4](#4-env-secrets-and-settings)). Newer consoles show the secret only once. If you lose it, open the client and click **+ Add secret**.

**Check:** **Clients** shows two rows, `emailctx-desktop` (Desktop) and `emailctx-mcp-login` (Web application).

## 4. `.env`: secrets and settings

Create `.env` in the repo root. It's gitignored, so never commit it or paste it into chat. Every `emailctx` command reads it on start, and variables already set in your shell take precedence.

```dotenv
# LLM key: the one matching `provider:` in config.yaml (you provide)
GEMINI_API_KEY=AIza...
# ANTHROPIC_API_KEY=sk-ant-...

# MCP server login: the Web application client from step 3b (you provide)
EMAILCTX_GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com
EMAILCTX_GOOGLE_CLIENT_SECRET=GOCSPX-...

# Who may sign in, comma-separated. Personal Gmail addresses must be listed exactly.
EMAILCTX_ALLOWED_EMAILS=alice@gmail.com,bob@gmail.com
# A whole Google Workspace domain can go here instead (never gmail.com)
EMAILCTX_ALLOWED_DOMAINS=

# The server's address as Claude sees it; must match the redirect URI in step 3b
EMAILCTX_PUBLIC_URL=http://localhost:8000
```

On macOS/Linux, run `chmod 600 .env client_secret.json` so only you can read them.

**The server reads `.env` only when it starts.** After changing it (for example, adding a teammate's email), stop the server with Ctrl+C and start it again.

## 5. `config.yaml`: LLM provider and models

```yaml
provider: gemini            # or: anthropic
models:
  gate: gemini-3.5-flash-lite
  extract: gemini-3.6-flash
  judge: gemini-3.5-flash-lite
```

Model names must exist **for your key**. Model names change often, so list the ones your key can use:

```bash
.venv/bin/python -c "from google import genai; [print(m.name) for m in genai.Client().models.list()]"
```

(Run it after `export GEMINI_API_KEY=...`.) What we saw on 2026-11-22: the `gemini-3-*-preview` names returned 404 (removed), `gemini-3.7/3.8-flash` returned 503 (overloaded), and `gemini-4-*` returned 429 (no quota on a free key). With `provider: anthropic`, use `claude-haiku-5-5` for gate and judge and `claude-opus-5-5` for extract.

Leave `google.scopes` with only `gmail.readonly` unless you turn on Drive output.

## 6. `registry.yaml`: your clients

The server only picks up mail it can place with a client. Each client has exactly one project:

```yaml
own_domains: [ourstudio.io]          # your team's domain, if you have one
companies:
  - id: acme
    name: Acme
    domains: [acme.com]              # mail from/to this domain → Acme
    contacts: [jane.acme@gmail.com]  # people writing from personal addresses
    projects:
      - id: acme
        name: Acme Customer Portal
        keywords: [Acme]             # a subject containing "Acme" → Acme
```

Load it, and again after every edit:

```bash
emailctx registry load registry.yaml
```

The repo's `registry.yaml` has three test clients (Acme, Nova, Orion), routed by **subject keyword**. Our test mail comes from personal Gmail addresses, whose domain says nothing about the client. **A test email must have the client's name in the subject**, e.g. `Acme: portal requirements`, or it's ignored.

**Check:** the output says `added/updated: ... companies N ...`.

## 7. Sign in the Gmail inbox (server host)

```bash
emailctx auth
```

1. A browser opens. Sign in as **the inbox** account.
2. On "Google hasn't verified this app", click **Advanced → Go to emailctx (unsafe)**. This is expected while the app is in Testing.
3. Allow read-only Gmail access. The tab says the flow is complete.
4. On macOS, if asked whether Python may use the Keychain, click **Always Allow**.

**Check:** the terminal prints `signed in as <inbox> (scopes: https://www.googleapis.com/auth/gmail.readonly)`.

The token is saved in the OS keyring (macOS Keychain or Windows Credential Manager), never in a file. So `emailctx auth` and `emailctx serve` must run **on the same machine, as the same OS user**. With an External/Testing app, Google expires the sign-in after **7 days**. When the server logs `not signed in`, run `emailctx auth` again. `emailctx auth --logout` removes the token.

## 8. Run the server

```bash
emailctx serve --auth google --poll-minutes 1
```

Expected output:

```
[auth] Google sign-in required; allowed: alice@gmail.com, bob@gmail.com
[gmail] checking for new client mail every 1 min
emailctx MCP server on http://127.0.0.1:8000/mcp (database .../emailctx.db)
Uvicorn running on http://127.0.0.1:8000
[gmail] inbox@gmail.com: incremental, 0 new, 0 with changes
```

- `--auth google`: everyone connecting must sign in with an allowed Google account, and every write records their email. Without it there's no login (fine for a solo local demo).
- `--poll-minutes 1`: checks Gmail every minute. Without it, the server **never reads the inbox**. Setting `gmail.poll_minutes` in `config.yaml` does the same.

Leave this terminal open. The `[gmail]` lines are how you see mail arrive.

## 9. Connect Claude Code

Every teammate does this on their own machine. Add the server once, for every folder (`--scope user`):

```bash
claude mcp add --scope user --transport http emailctx http://localhost:8000/mcp
```

(Use the tunnel URL from [step 10](#10-teammates-on-other-machines) instead of `localhost` if the server runs on someone else's machine.)

Then:

1. Run `claude`, then type `/mcp`. **emailctx** shows *needs authentication*.
2. Select it, choose **Authenticate**, and sign in with your Google account. It must be in `EMAILCTX_ALLOWED_EMAILS` **and** be a Google test user. Click **Advanced → Go to emailctx** if warned.
3. `/mcp` now shows **emailctx: connected**, with its tools (`list_companies`, `get_company_context`, ...).

**Check:** ask `What does Acme want?` without mentioning emailctx. Claude should call `get_company_context` and answer with requirement IDs and footnotes citing the source emails. The server tells every connected Claude to answer client questions from its tools, so you don't need to name it.

Things to try:

| You say | Claude calls |
|---|---|
| "What's still open for Acme?" | `get_company_context` / `list_items` |
| "Mark REQ-003 done, PR #12." | `set_item_status` (records your email) |
| "We need to ask Acme which SSO they use." | `list_questions`, then `log_question` |
| Paste a client email or meeting notes | `add_client_input` |
| "What exactly did they say about REQ-002?" | `get_source` |
| `/mcp__emailctx__company Acme` | loads the project and summarizes it |

Sign-ins are stored in `emailctx.db`, so restarting the server doesn't sign anyone out.

## 10. Teammates on other machines

`localhost:8000` only works on the server host's own machine. To let teammates in, expose it over HTTPS, for example with [ngrok](https://ngrok.com):

```bash
ngrok http 8000                      # gives https://<name>.ngrok.app
```

Then:

1. Add `https://<name>.ngrok.app/oauth/google/callback` to the Web client's **Authorized redirect URIs** ([step 3b](#3b-web-application-client-teammates-sign-in-to-the-mcp-server)).
2. In `.env`, set `EMAILCTX_PUBLIC_URL=https://<name>.ngrok.app`.
3. Restart with `emailctx serve --auth google --poll-minutes 1 --allowed-host <name>.ngrok.app`.
4. Teammates run `claude mcp add --scope user --transport http emailctx https://<name>.ngrok.app/mcp`.

## 11. End-to-end test

1. From any address, send mail **to the inbox** with a client name in the subject:
   - Subject: `Acme: portal requirements`
   - Body: `For the pilot, users must be able to export their invoice history as CSV. Please also add a dark mode toggle.`
2. Within a minute or two, the server prints:
   ```
   [gmail] inbox@gmail.com: incremental, 1 new, 1 with changes
     acme: REQ-001 add, REQ-002 add
   ```
3. In Claude Code, ask `Does Acme want dark mode?`. Expected answer: yes, REQ-002, citing that email.

A message with no requirements in it (e.g. "this is a test") is logged as `1 new, 0 with changes`. The model found nothing to record, so nothing is invented.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Browser: **Access blocked: emailctx has not completed the Google verification process** | The account isn't a test user. Add it under **Audience → Test users** ([step 3](#3-google-cloud-project-server-host-once)). |
| Browser: **access_denied: you@x.com is not allowed on this server** | Not in `EMAILCTX_ALLOWED_EMAILS` / `_DOMAINS`. Add it to `.env` **and restart the server**. |
| Still refused after adding the email | The server wasn't restarted, or an old server is still on port 8000. Run `lsof -i :8000` (macOS) and stop it. |
| Google error **redirect_uri_mismatch** | The Web client's redirect URI isn't exactly `<EMAILCTX_PUBLIC_URL>/oauth/google/callback`. |
| `error: --auth google needs EMAILCTX_GOOGLE_CLIENT_ID...` | `.env` is missing, unsaved, or not in the folder you run from. |
| No `[gmail]` lines at all | Started without `--poll-minutes`. |
| `[gmail] ... 0 new` after sending mail | The subject doesn't contain a client keyword, or the mail went to a different inbox. |
| `[gmail] not signed in to Google` | The 7-day Testing expiry, or the server runs as a different OS user. Run `emailctx auth`. |
| `sync stopped ... no Gemini API key` / `no Anthropic API key` | Add the key matching `provider:` to `.env`, then restart. |
| `API error: 404 NOT_FOUND ... models/<name>` | That model name doesn't exist for your key. Pick one from the list in [step 5](#5-configyaml-llm-provider-and-models). |
| `API error: 503 UNAVAILABLE` / `429 RESOURCE_EXHAUSTED` | Overloaded model / no quota. The sync retries the same email next round; switch models if it persists. |
| Claude says it has "no record" of something the server logged | Claude didn't call the tools. Check `/mcp` shows emailctx **connected**; sign in again if it says needs authentication. |
| `/mcp` shows emailctx only in one folder | It was added without `--scope user`. `claude mcp remove emailctx` and add it again with `--scope user`. |

## What's in the repo, and what isn't live data

- The challenge's **sample project** (`dummy_data/`, `expected_context/`, `eval/facts/`) is not on `main`. It lives on the `sample-data` branch, for `emailctx load-sample` and `emailctx eval run`. It's never the live client state: live state is only in `emailctx.db`, served through the MCP tools.
- `companies/<id>/context.md` is a rendered copy of each client's state (gitignored).
- Secrets and local state are gitignored: `.env`, `client_secret*.json`, `*.db`, `companies/`.
- Design and internals: [DESIGN.md](DESIGN.md). Challenge brief: [challenge-8.md](challenge-8.md).
