# Email-to-Context MD: System Design

Nov 21, 2026 · @mustafa

## Overview

The system reads a connected Gmail account, sorts each email into the right company (each company has exactly one project), and keeps one `<Company>.md` per company describing its requirements. Each company has a Google Drive folder holding that file, a readable copy of every email routed to the company, and those emails' attachments (e.g. meeting-summary PDFs). A company's first file is built from the last 100 days of its mail; after that the file changes only when a requirement is added, modified or removed.

The requirements database holds no email bodies: emails are checked for requirement changes in memory, and their copies live only in the company's Drive folder.

**Core principle:** the LLM never writes the markdown. It only proposes structured changes to a requirements store. Code validates those changes, and a deterministic template renders the `.md` from the store. If no material change passes validation, the rendered output is byte-identical and the file is not touched.

Goals:

- One accurate, cited requirements doc per project
- No churn from rewording, noise emails or quoted replies
- Every requirement traceable to the email or Drive file it came from

## Pipeline

Only validated requirement changes reach the project md.

```
Sources ──► Ingest ──────────► Clean ───────────────► Route
(Gmail +    (incremental       (strip quotes,         (company, project)
 Drive)      sync)              MIME)                    │
                                                         ├── none      ──► Unassigned (no project, stop)
                                                         ├── unsure    ──► Review queue (a person assigns) ──┐
                                                         └── confident ──► Project segments (one per project)◄┘
                                                                                     │
┌─ Per project segment ──────────────────────────────────────────────────────────────▼──────┐
│  Gate ──────────► Extract ───────────► Validate ─────────► Store ──────────► Render        │
│  (skip noise     (LLM to JSON ops,     (evidence check,    (SQLite,          (Jinja,       │
│   and FYIs)       MCP tools)            dedupe)             change log)       hash compare)│
└───────────────────────────────────────────────┬────────────────────────────────┬──────────┘
                                                │ 0 valid ops                    │ only if hash differs
                                                ▼                                ▼
                                          No change                         context.md
                                          (md not touched)                  (cited, git commit)
```

Routing runs once per email. Everything after it runs per project segment, and a project's md is rewritten only when validated ops change the rendered output.

## Access: direct APIs for sync, MCP for the agent

Use direct Google APIs where the pipeline has to be deterministic, and Google Workspace MCP tools where an agent needs to explore.

| Layer | Uses | Why |
|---|---|---|
| Sync | Gmail API (`history.list`), Drive API (`changes.list`) | Must never miss or re-read an email; MCP tool sets usually expose search and read only, not history or watch |
| Context gathering | Google Workspace MCP (official remote servers or workspace-mcp) | The extraction agent can open a Drive doc an email links to, or look up a related thread |

Auth:

- One Google Cloud project with the Gmail and Drive APIs enabled, plus the Gmail and Drive MCP services if you use the official MCP servers
- OAuth 2.0: `gmail.readonly`, plus `drive.file` (the app can only see and edit the folders and files it created itself)
- Refresh token stored encrypted (OS keyring)
- MCP tools can also be called straight from code without an LLM (e.g. langchain-mcp-adapters). Check the server's tool list before relying on it for sync.

## Ingestion

Emails and Drive files are two source types feeding the same pipeline. Each sync pulls only what changed since the last run.

### Gmail

- Scope per account: a Gmail query that covers all tracked projects (e.g. specific labels, client domains). Routing to a project happens later.
- Backfill: `messages.list` with the query, then `messages.get` per message
- Incremental: store the mailbox `historyId` and call `history.list` from it each run. Poll every few minutes at first. Move to `users.watch` + Pub/Sub push if you need real time.
- Idempotency: skip any message ID already in `processed_messages`

### Drive

- Sources: each project's Drive folder, plus any Drive links found in its emails
- Change detection: `changes.list` with a stored page token, or compare `modifiedTime` / `revisionId`
- On change: export to text, diff against the last stored revision, send only the changed sections downstream

## Cleaning

Stripping quoted replies matters more than it looks. Without it, every reply re-feeds old requirements and the LLM treats them as new.

- Decode MIME, convert HTML to text
- Strip quoted replies (`On ... wrote:`, `>` lines) and signatures
- Extract text from PDF and doc attachments if requirements live there
- Text of PDF and text attachments is appended to the cleaned body (`--- Attachment: <name> ---`), so decisions in meeting summaries count and their quotes pass the evidence check.
- Keep the cleaned text in memory only: the validation step checks evidence quotes against it, then it is discarded (the email's copy lives in Drive, not in the database). To catch quoted history in later replies, only SHA-256 hashes of each cleaned paragraph are stored per thread.

## Project routing

Every cleaned email is assigned to zero, one or several projects before any extraction runs. Cheap rules go first, the LLM handles only the emails the rules can't decide, and low-confidence emails go to a person. A low-confidence email never writes to any project's md.

Two levels: company, then project. Email domains identify the company first. That narrows the candidates to that company's projects, so later steps choose between a few projects instead of all of them.

### Company registry

- `company_id`, name
- Every domain the company uses (e.g. acme.com, acme.ca, acmecorp.com)
- Individual addresses for contacts on personal providers (e.g. jane.acme@gmail.com)
- Its projects (each project carries a `company_id`)

### Resolving the company from an email

1. Collect addresses from From, To, Cc and Reply-To. Use the address, never the display name.
2. Drop your own organization's domains.
3. Normalize each domain to its registrable domain with the Public Suffix List (tldextract): `mail.eu.acme.co.uk` becomes `acme.co.uk`.
4. Personal providers (gmail.com, outlook.com, hotmail.com, yahoo.com, icloud.com) say nothing about the company. Match those on the full address in `company_contacts` instead.
5. Priority: the sender's company if the sender is external. If you sent the email, use the recipients' companies.
6. Several companies on one email (e.g. a client with their vendor in Cc): the sender's company wins, then the company with active projects. Still tied goes to review.
7. Unknown domain: queue it as a pending company. Once a person approves it, it joins the registry and future emails from that domain match automatically.
8. Optional hardening: trust the sender's domain only when the `Authentication-Results` header shows `dmarc=pass`, so a spoofed From can't route into a client's project.

### Project registry (one profile per project)

- `project_id`, name, short description
- Gmail label, if used
- Known participants (emails, domains)
- Keywords and codenames (e.g. "Atlas", "PO-4471")
- Drive folder ID
- Running summary of current requirements, generated from the store

### Routing cascade (stop at the first confident hit)

1. **Thread inheritance.** If the thread is already assigned, the reply inherits that project. This handles most traffic once a thread has been routed once.
2. **Explicit signals.** A Gmail label, a project code in the subject, or a link to a file in a known project folder. Treat these as certain.
3. **Company match.** Resolve the company from domains (above). If the company has exactly one active project, assign it. Otherwise keep only that company's projects as candidates. No company found (internal-only email): all projects stay candidates.
4. **Participants.** Within the candidates, assign if a specific contact maps to exactly one project (e.g. Jane only works on Atlas). Otherwise fall through.
5. **Embedding similarity.** Compare the email to the candidate projects' profiles and requirement summaries. Assign only if the top score is high and clearly beats the second.
6. **LLM classifier.** Gets the email plus the top 3 candidate profiles, not every project. Returns `{"company_id", "assignments": [{"project_id", "confidence", "reason"}], "none": bool}`.
7. **Review queue.** Anything under the threshold, where the LLM and embeddings disagree, or from an unknown company waits for a person.

### Emails about multiple projects

- Allow multi-label output. The LLM splits the email into one segment per project, and each segment runs through that project's own gate, extract and validate steps.
- Store each segment with its `msg_id` so the evidence check still works.

### "None" is a valid answer

- Most emails belong to no project. They go to unassigned and stop. This doubles as a first relevance filter.

### Feedback loop

- When someone reassigns an email in the review queue, store the correction and turn it into a rule where possible (new participant mapping, new keyword, pinned thread). Over time the rules catch more and fewer emails reach the LLM.
- Log every routing decision with the step that made it (thread, label, participant, embedding, llm, manual) so misroutes can be traced.

> A wrong route is worse than a missed one. It pollutes another project's requirements, and the evidence check won't catch it because the quote really is in the email. Start with high thresholds and loosen them only after the logs show the router is accurate.

## Gate, extraction and validation

These three steps run per project segment. Validation is where "only change when a requirement changes" is enforced.

### Relevance gate

- A cheap LLM call asks: does this segment contain a requirement, decision or scope change?
- "Thanks", meeting scheduling and FYIs stop here

### Extraction

- Input: the cleaned segment plus the project's current requirements (IDs + text)
- Output: JSON ops only, no prose. Allowed ops: `add`, `modify`, `remove`, `none`

```json
{"ops": [
  {"op": "modify", "req_id": "REQ-007", "text": "Export must support CSV and XLSX",
   "evidence": "we also need xlsx export", "msg_id": "18c2..."},
  {"op": "add", "text": "...", "category": "non-functional", "evidence": "...", "msg_id": "..."}
]}
```

- The agent can use MCP tools here, e.g. to open a Drive doc the email links to

### Validation

- `evidence` must appear as a substring of the cleaned source text, or the op is rejected. This blocks hallucinated requirements.
- `modify` and `remove` must reference an existing requirement ID
- `add` is checked for near-duplicates (embeddings or an LLM comparison); duplicates are dropped or converted to modify
- `modify` where the normalized text is unchanged is dropped as a rewording
- Conflicts (a newer email contradicts an older one): latest wins, flagged under "Needs confirmation" in the md
- Zero surviving ops means the md is not touched

## Data model

SQLite is the source of truth. The `.md` files are views of it. Postgres works the same way if this moves to a server. No table holds email bodies, addresses or evidence quotes.

| Table | Key columns | Purpose |
|---|---|---|
| companies | company_id, name, status (active, pending) | Company registry; pending = unknown domain awaiting approval |
| company_domains | domain, company_id | Registrable domains per company, all aliases |
| company_contacts | email, company_id | Contacts on personal providers (gmail.com etc.) |
| own_domains | domain | Your organization's domains, ignored when resolving |
| projects | project_id, company_id, name, description, label, drive_folder_id, req_summary | Registry profiles used by the router |
| project_rules | project_id, rule_type (participant, keyword, thread), value | Deterministic routing rules, grown from review corrections |
| thread_assignments | thread_id, project_id | Thread inheritance |
| routing_log | msg_id, company_id, project_id, step, confidence, timestamp | Every routing decision and the step that made it |
| review_queue | msg_id, kind (project, new company), candidates, reason, source_ref, status | Low-confidence emails and unknown domains waiting for a person; the email is re-read from source_ref (Gmail or file) |
| conflicts | project_id, req_id, kind (contradiction, stale), kept_text, other_text, kept_source_id, other_source_id, status | "Needs confirmation" items |
| segments | segment_id, msg_id, project_id | Which projects a message was routed to (segment text stays in memory) |
| requirements | req_id, project_id, text, category, status, version, source_ids, updated_at | Current requirements per project |
| change_log | req_id, op, old_text, new_text, source_id, timestamp | History behind every change |
| messages | msg_id, gmail_id, thread_id, subject, date | Email metadata for idempotency, threading and citations; no body |
| thread_paragraphs | thread_id, para_hash, first_seen | Paragraph hashes for dropping quoted history; no text |
| processed_messages | msg_id, outcome | Idempotency |
| drive_revisions | file_id, revision_id, text_hash | Last seen version of each Drive file |
| sync_state | account, history_id, drive_page_token, last_run | Incremental sync cursors |

Implementation additions (see Decisions below): `requirements` keyed on `(project_id, req_id)` with a `normalized_text` column, and `change_log` rows carrying `project_id` and an autoincrement `change_id`.

## Rendering and versioning

Each company's `context.md` is rendered by a template from the store, so the same requirements always produce the same bytes.

- Template (Jinja): a section per project; within it, requirements sorted by ID and grouped by category; a "Needs confirmation" section for conflicts; and a company-wide changelog
- Sources are markdown footnotes on each requirement and changelog entry: `[^1]: Email "<subject>", <date>, ID <Message-ID>`. The ID is shown only when the email had a Message-ID. Footnotes are numbered by email date, so existing numbers don't shift when newer emails arrive.
- Published to `s3://<bucket>/<prefix><company_id>/context.md` and/or `<local_dir>/<company_id>/context.md`. Hash the rendered output and compare it to each destination (S3 keeps the SHA-256 in object metadata). Write only where they differ.
- Keep the md files in a git repo, one file per project. Each update is a commit like `REQ-007 modified (email 18c2..., Nov 20)`, giving history and easy rollback.
- Human in the loop (optional): open a PR instead of committing directly

## Stack, build order and risks

**Stack:** Python, google-api-python-client, SQLite, Pydantic for the op and routing schemas, Jinja for rendering, an embeddings model for routing and dedupe. LangGraph fits the per-segment flow: gate, extract, validate and apply as nodes, with validation able to send ops back for a retry.

### Build order

Gmail sync comes before routing so that real synced emails can serve as routing test data.

1. Core pipeline on local `.eml` files for one project: clean, gate, extract, validate (evidence check, reword/dedupe judge), store, render
2. Gmail: OAuth, backfill, incremental sync with `historyId`, idempotency
3. Company and project registry, routing cascade steps 1–4 (thread, explicit signals, company match, participants), review queue
4. ~~Embedding + LLM routing, multi-project segmentation~~ Dropped: routing stays rule-based. The email's domain identifies the company and its project; no classifier runs. Emails the rules can't place go to the review queue.
5. Git commit per changed `context.md`, conflict flagging under "Needs confirmation"
6. Drive folder per company (the `.md`, email copies, attachments), first build from the last 100 days, meeting-summary PDFs read as part of their email. Still open: MCP tools for the extraction agent

### Risks

| Risk | Mitigation |
|---|---|
| Email routed to the wrong project | Company narrowing, high thresholds, review queue, routing log |
| Personal or shared domains (gmail.com) misidentify the company | Free-provider list, contact-level mapping in company_contacts |
| Spoofed sender routes into a client project | Address not display name; optional dmarc=pass check |
| Hallucinated requirement | Evidence substring check |
| Old requirements re-extracted from quoted replies | Quote stripping in cleaning |
| Reworded duplicates churn the md | Dedupe + unchanged-text check + deterministic render |
| Missed or double-processed emails | historyId sync + processed_messages |
| Contradictory emails | Latest wins, flagged for confirmation |

## Decisions (implementation)

- **Evidence matching.** Both the evidence and the cleaned source text are normalized before the substring check: NFKC, strip zero-width characters, map curly quotes and dashes to ASCII, collapse whitespace. Matching stays case-sensitive. The evidence string is used only for this check and is not stored.
- **Reword and dedupe.** First a cheap exit: drop a `modify` whose normalized text equals the current text, and an `add` whose normalized text equals an existing requirement. Then an LLM judge compares the proposed text with existing requirements and returns `{duplicate_of, same_meaning}`. For `modify`, same meaning means the op is dropped. For `add`, a duplicate with the same meaning is dropped; a duplicate with a different meaning becomes a `modify` of that requirement. An `Embedder` interface exists from Phase 1 with a no-op implementation (every requirement is a candidate). In Phase 4 sentence-transformers plugs in as a prefilter so the judge sees only the top-k similar requirements.
- **Categories.** A fixed set from config. Anything else the LLM returns is mapped to `other`, not rejected.
- **Message IDs.** The RFC `Message-ID` header is the dedupe key (`msg_id`). Gmail's own message ID lives in its own column. If `Message-ID` is missing, `msg_id` falls back to a SHA-256 of from + date + subject + cleaned body.
- **Requirement IDs** are assigned by code (`REQ-001`, `REQ-002`, … per project) and never reused. The LLM never picks the ID for an `add`.
- **Remove** sets `status = removed`; rows are never deleted, so history and citations survive.
- **Where emails live.** The database never holds email bodies, addresses or evidence quotes. A readable `.txt` copy of every email routed to a company (headers, then the full body) and its attachments are stored in that company's Drive folder. Emails waiting in review are not copied until a person assigns them (their company is unknown). Evidence quotes are validated in memory and discarded; citations are the email's ID (when it has a Message-ID), subject, date and attachment names.
- **One project per company, one file per company.** Each company has exactly one project; the registry and the CLI refuse a second one. So a company's `context.md` is simply its project's requirements (categories as `##` sections, no project heading). `--company` alone is enough on `ingest-file` and `review assign`: it means the company's project, created with the company's id and name if needed.
- **Output.** Google Drive is the main destination (`output.drive`), described below. S3 (optional) is enabled by setting `output.s3.bucket` in config; credentials come from boto3's standard chain and are never logged. Turning on bucket versioning gives history and rollback. Each S3 object carries a `change` metadata line describing the change and its source email. Optionally (`output.git_commit`), the local output folder is its own git repo with one commit per changed file, e.g. `atlas: REQ-001 modify (email <id>, 2026-11-19)`.

### Gmail (Phase 2)

- **Auth:** `emailctx auth` runs the installed-app OAuth flow with an OAuth client of type "Desktop app" (`google.client_secrets`, gitignored). Scopes come from config and only `gmail.readonly`, `drive.readonly` and `drive.file` are accepted; the app requests `gmail.readonly` and `drive.file`. The token (with refresh token) is stored in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service), refreshed automatically, and never written to a project file or printed. `emailctx auth --logout` removes it.
- **Scope of mail:** `gmail.query` (a Gmail search) selects tracked mail. Backfill takes at most `gmail.backfill_max` of the most recent matches (default 200) so a first sync can't send a whole mailbox through the LLM; they are processed oldest first.
- **Incremental:** `history.list` (messageAdded) from the stored historyId. History can't filter by query, so each new message is checked with one `messages.list` narrowed to its timestamp. Drafts, spam and trash are skipped. A 404 (expired historyId) falls back to a backfill.
- **Never miss, never repeat:** the cursor is taken from `getProfile` before a backfill and saved only after every message in the batch went through; on failure the next run resumes from the old cursor. Messages already processed (by Gmail ID) are skipped without being downloaded, and the RFC Message-ID dedupes mail seen both as a file and through Gmail.
- Gmail label IDs are mapped to label names, so a project's `label` is an explicit routing signal. Review items from Gmail keep `gmail:<id>` and are fetched again on assign.

### Drive folders and the first build

- **Layout:** `<root>/<Company>/` (root defaults to "Client Context", created by the app in My Drive) holds `<Company>.md`, `Emails/` (`<date> <subject>.txt`) and `Documents/` (`<date> <attachment name>`). Every file and folder carries private appProperties (kind + key: company id, or a hash of the Message-ID); that is how the app finds them again, so no Drive IDs are stored locally and people may move or rename things. A company renamed in the registry has its folder and file renamed.
- **Writes:** email copies and attachments are written once (keyed by Message-ID, so retries never duplicate). The `.md` is updated in place when its SHA-256 changes; Drive keeps its revision history. The email copy is written before the pipeline commits, so a failure leaves nothing half-done and the next run retries.
- **First build:** at the start of every `emailctx sync` (or `emailctx bootstrap`), each company whose `.md` is missing from Drive (never built, or deleted/trashed) is built from the last `bootstrap.days` (100) days of Gmail: mail from/to/cc its domains and contacts, subject keywords, and meeting-bot emails naming it, at most `bootstrap.max_per_company`, oldest first, through the normal pipeline. Emails that route to another company are left for that company's build. The `.md` is created even if nothing was found. A rebuild after deletion re-renders from the database without re-reading already-processed mail. On the very first sync the per-company builds replace the plain backfill; the Gmail cursor is taken before they run.
- **Incremental query:** with `gmail.query: null` (default) the sync query is built from the registry (all companies' domains, contacts, keywords and meeting bots). Set a query such as `label:clients` to also catch mail from unknown domains (which then go to review as new companies).
- **Meeting bots** (`routing.meeting_bot_domains`: fireflies.ai, otter.ai, …): the sender's domain carries no company signal and never becomes a pending company. The subject, body, attachment names and attachment text are searched (word boundaries, terms of 3+ characters) for each company's name and id, its project's name, id and keywords, and its domains. One company named → assigned (step `meeting_bot`); several → review; none → the usual rules (usually review).

### Loading a company's file into a Claude session

- `emailctx mcp` is a read-only MCP server (stdio) registered once at user scope (`claude mcp add emailctx --scope user -e EMAILCTX_CONFIG=<config.yaml> -- <venv>/emailctx.exe mcp`), so every new Claude Code session has it. Tools: `get_company_context(company)` returns `<Company>.md` from Drive; `list_companies()`. Prompt: `/mcp__emailctx__company <name>` loads the file and summarizes it.
- Lookup lists the app's `.md` files straight from Drive (no local database needed): case-insensitive, `.md` optional, a unique part of the name is enough; ambiguous or unknown names return the list of companies that do have a file.
- `emailctx context <name>` prints the same file in a terminal; `emailctx context` lists companies.

### Routing (Phase 3)

- **Registry** comes from a YAML file (`emailctx registry load registry.yaml`): own domains, companies with domains and personal-provider contacts, projects with Gmail label, subject keywords, participants (address or domain) and Drive folder. Loading is an idempotent upsert and never deletes rules learned from review.
- **Pending companies** are created with `company_id` = the unknown registrable domain. A later registry entry listing that domain adopts it; a review assignment approves it (activated if it is the project's company, otherwise folded into the project's company as an alias domain).
- **Ignored companies:** dismissing a review item with `--ignore-domains` marks its pending companies `ignored`; their addresses then carry no company signal, like personal providers.
- **Explicit signals:** keywords match the subject only, case-insensitive, on word boundaries (`PO-4471` does not match `PO-44712`). A Drive-folder ID anywhere in the body counts. Several projects hit at the same step go to review; nothing is assigned on a tie.
- **Review without storing email:** a review item keeps a source reference (`eml:<path>`, later `gmail:<id>`). Assigning re-reads the email from there, pins its thread, records the external sender as a participant (and as a company contact if on a personal provider), then runs the pipeline.
- **No classifier.** Cascade steps 5–6 (embeddings, LLM classifier) and multi-project segmentation are not built: the email's domain names the company, and the company has one project. Participant rules still place mail with no company domain (e.g. a contractor on gmail.com). Emails no rule can place go to review; none are auto-marked unassigned.

### Conflicts (Phase 5)

- **Latest wins by email date, not processing order.** A modify or remove from an email dated before the requirement's last change is not applied; it is recorded as a `stale` conflict.
- **Contradictions:** the judge also returns `contradicts`. A modify that reverses the current text (not one that extends it) is applied and recorded as a `contradiction` conflict.
- Open conflicts render under "Needs confirmation", citing both emails. `emailctx conflicts confirm C<n>` accepts the current text; `--use-other` switches to the other email's version, cited to that email.
- **Models and thresholds** live in `config.yaml`. Extraction defaults to `claude-opus-5-5`; the gate and the judge default to `claude-haiku-5-5`.
