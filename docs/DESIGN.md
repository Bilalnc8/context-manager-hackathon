# Email-to-Context MD: System Design

Nov 21, 2026 · @mustafa

## Overview

The system reads a connected Gmail account, sorts each email into the right company (each company has exactly one project), and keeps one `<Company>.md` per company describing its requirements. Each company has a Google Drive folder holding that file, a readable copy of every email routed to the company, and those emails' attachments (e.g. meeting-summary PDFs). A company's first file is built from the last 100 days of its mail; after that the file changes only when a requirement is added, modified or removed.

The database is the single source of truth, including the cleaned text of every client input (synced email, `.eml` file, or text pasted into a Claude session), so every citation can be opened. Google Drive is an optional published view, off by default.

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
- Store the cleaned text (quotes and signatures already removed, attachment text appended) with the message, so citations can be opened (`get_source`). Evidence quotes are checked against it in memory and not stored. To catch quoted history in later replies, SHA-256 hashes of each cleaned paragraph are stored per thread.

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

- A cheap LLM call asks: does this segment contain a requirement, decision or scope change, or change where the project stands (a key date or its approval, a concern or blocker raised or resolved, a next action)?
- "Thanks", meeting logistics and FYIs stop here; the gate's reason is reported with the result

### Extraction

- Input: the cleaned segment plus the project's current requirements (IDs + text)
- Output: JSON ops only, no prose. Allowed ops: `add`, `modify`, `remove`, `resolve` (close a concern or action), `answer` (a client question), `none`; plus `skipped`: every other sentence, quoted, with the reason it changes nothing (see Coverage)

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
- `resolve` must name an active concern or action; a deliverable is never closed by email (its status is set by the team). Already resolved, or from an email older than the item's last change: dropped
- Zero surviving ops means the md is not touched

### Coverage: nothing in an email is missed

- Code splits the cleaned text into sentences (wrapped lines joined; list items and headings start new ones; "Dec. 5" and "e.g." don't split). Sentences under `pipeline.coverage_min_words` words (4) are not checked.
- A sentence is accounted for when a verbatim quote overlaps it: the evidence of an op that wasn't rejected (accepted, dropped as a rewording or duplicate, stale, or an answer), or a `skipped` quote that itself passes the evidence check.
- Unaccounted sentences go back to extraction with the rejected ops (within `max_extract_retries`), then are reported with the result: `add_client_input` and the CLI list what was skipped and why, and anything **not captured** so a person can look. They don't change the file.

## Data model

SQLite is the source of truth. The `.md` files are views of it. Postgres works the same way if this moves to a server. `messages` holds each input's cleaned text and sender; no table holds raw emails, quoted history, signatures or evidence quotes.

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
| requirements | req_id, project_id, text, category, status, version, source_ids, updated_at, delivery, delivery_note | Current requirements per project; `delivery` is open, in_progress, done or verified |
| change_log | req_id, op, old_text, new_text, source_id, timestamp | History behind every change |
| messages | msg_id, gmail_id, thread_id, subject, date, sender, body | Idempotency, threading and citations; `body` is the cleaned text the pipeline read |
| thread_paragraphs | thread_id, para_hash, first_seen | Paragraph hashes for dropping quoted history; no text |
| processed_messages | msg_id, outcome | Idempotency |
| drive_revisions | file_id, revision_id, text_hash | Last seen version of each Drive file |
| sync_state | account, history_id, drive_page_token, last_run | Incremental sync cursors |
| status_changes | project_id, req_id, old_status, new_status, note, changed_by, changed_at | Delivery status history (see the MCP server section) |
| questions | project_id, question_id, text, req_id, status (open, asked, answered, withdrawn), asked_at, answer, answer_source_id, answered_by | Questions only the client can answer |

Implementation additions (see Decisions below): `requirements` keyed on `(project_id, req_id)` with a `normalized_text` column, and `change_log` rows carrying `project_id` and an autoincrement `change_id`.

## Rendering and versioning

Each company's `context.md` is rendered by a template from the store, so the same requirements always produce the same bytes.

- Template (Jinja): a header (project, client domain, status counts, last change), then most actionable first: Key dates, Open concerns, Questions for the client, Needs confirmation, Deliverables (one subsection per deliverable category, each item with its status), Next actions, Out of scope, Decisions, Answered by the client, Resolved and completed, Changelog. Items are sorted by ID within a section
- "Last change" is the date and subject of the newest source behind a change, never the wall clock, so an email that changes nothing leaves the file byte-identical
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
- **Categories.** A fixed set from config. Anything else the LLM returns is mapped to `other`, not rejected. Two kinds: deliverables (functional, non-functional, data, integration, ui, constraint, other), tracked open / in progress / done / verified; and tracking items that say where the project stands (`milestone`: a key date and its approval state; `decision`; `out-of-scope`: excluded or deferred work, kept visible rather than removed; `concern`: an open client concern, pending decision, blocker or risk; `action`: "Owner (Org): what, by when"). Proposals and requests for discussion are recorded as concerns until decided, never as deliverables.
- **Closing concerns and actions.** An email can `resolve` a concern or action (delivery `done`, shown as resolved, logged in `status_changes` as set by `email <msg_id>`); the team can also close one with `set_item_status`. A modify of a resolved concern reopens it, like finished work. Delivery status of deliverables stays the team's: no email sets it.
- **Message IDs.** The RFC `Message-ID` header is the dedupe key (`msg_id`). Gmail's own message ID lives in its own column. If `Message-ID` is missing, `msg_id` falls back to a SHA-256 of from + date + subject + cleaned body.
- **Requirement IDs** are assigned by code (`REQ-001`, `REQ-002`, … per project) and never reused. The LLM never picks the ID for an `add`.
- **Remove** sets `status = removed`; rows are never deleted, so history and citations survive.
- **Where emails live.** Each input's cleaned text and sender are stored with its message (changed from the first design, which kept no bodies: once the MCP server became the shared store, pasted input existed nowhere else). Gmail keeps the originals of synced mail. With Drive output on, a readable `.txt` copy of every email routed to a company and its attachments also go to that company's Drive folder. Evidence quotes are validated in memory and discarded; citations are the email's ID (when it has a Message-ID), subject, date and attachment names, and `get_source` opens the stored text.
- **One project per company, one file per company.** Each company has exactly one project; the registry and the CLI refuse a second one. So a company's `context.md` is simply its project's items (sections as `##`, no project heading). `--company` alone is enough on `ingest-file` and `review assign`: it means the company's project, created with the company's id and name if needed.
- **Output.** The local folder (`output.local_dir`) is on by default; Google Drive (`output.drive`, described below) is optional and off by default, since the MCP server reads the database directly. S3 (optional) is enabled by setting `output.s3.bucket` in config; credentials come from boto3's standard chain and are never logged. Turning on bucket versioning gives history and rollback. Each S3 object carries a `change` metadata line describing the change and its source email. Optionally (`output.git_commit`), the local output folder is its own git repo with one commit per changed file, e.g. `atlas: REQ-001 modify (email <id>, 2026-11-19)`.

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

### The MCP server: one shared tracker for every Claude session

The challenge (`docs/challenge-8.md`) needs more than a requirements list: any agent in any new session must know what is **done, open and verified**, and what the client **already answered**. So the MCP server is the product's front door, hosted once for the team and connected from Claude Code, Claude Desktop or claude.ai.

```
Claude Code / Claude Desktop / claude.ai
        │  MCP over HTTPS (streamable HTTP); Google sign-in = login only (phase 2)
        ▼
emailctx serve ── tools ──► central store (SQLite) ──► <Company>.md (local / Drive / S3)
        ▲
client input: pasted into a session (add_client_input), .eml files, team Gmail sync
```

- **Transport.** `emailctx serve` runs streamable HTTP at `/mcp` (Claude Code: `claude mcp add --transport http emailctx http://localhost:8000/mcp`). `--allowed-host` accepts a tunnel's hostname (DNS-rebinding protection is on). `emailctx mcp` runs the same server over stdio for one machine.
- **Reads the store, not Drive.** Every tool works on the central database, so all agents and teammates see the same state and Drive is optional. `get_company_context` renders the `.md` fresh from the store (the same bytes the publishers write).
- **Tools.** Read: `list_companies` (counts per status), `get_company_context`, `list_items(status?)`, `list_questions`, `get_source` (the text behind a footnote). Write: `set_item_status`, `log_question`, `mark_questions_asked`, `record_answer`, `withdraw_question`, `add_client_input`. Prompt: `/mcp__emailctx__company <name>`. The server instructions tell the agent the loop: load context first, mark work done, check questions before asking, capture pasted client input.
- **Delivery status** (`requirements.delivery`: open → in_progress → done → verified) is set by the team, never by the email pipeline, with every change in `status_changes` (who, when, note). Two rules keep finished items finished: an `add` the judge maps onto a done/verified requirement stays a new item (new work on top of finished work) unless it contradicts it; a `modify` of finished work (the client changed what was delivered) reopens it, logged as `changed_by = email <msg_id>`.
- **Client questions** (`questions`, `Q-001` per project): `log_question` never logs the same normalized text twice, and returns the earlier question (with its answer, if any) instead, so the client is never asked twice. `mark_questions_asked` refuses a question that isn't open. Answers arrive two ways: an email or pasted input through the pipeline (the gate and extraction see the waiting questions; extraction returns an `answer` op with evidence, validated like every other op), or `record_answer` for an answer given on a call.
- **Pasted input** (`add_client_input`) is wrapped as an email (From the company's domain, no Message-ID, date default now) and runs the normal pipeline with the company given, so it gets the evidence check, dedupe and conflict rules. Same text, subject, sender and date → same hash ID → processed once.
- **Many users, one writer.** `emailctx serve` is one process that owns the database: every tool call and the Gmail poller share one runtime with one write lock. A write (an email's whole ingest, a status change, a logged question) holds it from its first database write through rendering and publishing the file, so writes take turns instead of failing on SQLite's lock, and an older render can never overwrite a newer file. SQLite runs in WAL mode, so reads never wait, and `busy_timeout` (30 s) covers a second process, such as a CLI ingest while the server runs. A write that arrives during an email's LLM calls waits for them (seconds).
- **Gmail in the server.** `emailctx serve --poll-minutes 5` (or `gmail.poll_minutes`) runs `GmailSync` in a background thread on the server's runtime: once at start, then every N minutes. It needs `emailctx auth` once on the server machine, signed in as the shared inbox. Errors are logged and retried next round; the cursor only advances after a batch fully succeeds.
- **Identity.** Writes record `current_user()`: the signed-in user once Google sign-in is on (phase 2), `agent` until then.
- `emailctx context <name>` still prints a company's file from Drive in a terminal.

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

### The sample project and `emailctx eval`

- `dummy_data/` is the company's sample: `domain_project_mapping.yaml` (internal domain + one project per client domain), `emails.jsonl` (with Message-ID and In-Reply-To), and six meeting write-ups. `expected_context/<project>.md` is the expected outcome.
- `emailctx load-sample [dummy_data]` loads it into the configured store for a demo: the registry, then every email and meeting write-up in date order through the normal pipeline, routing included. Meeting write-ups arrive as email from a note-taker address (`notetaker.example`, treated as a meeting bot), so they are routed by the company they name. Each input is also written as an `.eml` under `sample-eml/` next to the database. Running it again changes nothing.
- `emailctx eval run` does the same in a fresh store under `eval/run/` (never the real database; the folder is cleared only if the command created it), then reports: routing against `expected_project_id`, unaccounted sentences, rejected ops, whether a second pass changes anything, and a fact score per project. The facts are a checklist per expected file (`eval/facts/<project>.yaml`, drafted with `emailctx eval draft-facts`); an LLM grades each as present, missing or contradicted and must quote the generated file verbatim, or the fact counts as missing. Report: `eval/run/report.md`, next to the generated files. It calls the Anthropic API (the `grade` model defaults to the extraction model).

## Google auth: setup and handoff

There are two separate Google setups. They can share one Google Cloud project, as two OAuth clients:

| | 1. Inbox reading | 2. MCP server user login |
|---|---|---|
| Purpose | emailctx reads client email from one shared inbox | Teammates prove who they are when Claude connects |
| OAuth client type | Desktop app (`client_secret.json`) | Web application |
| Scopes | `gmail.readonly` | `openid`, `email` |
| Who signs in | The shared inbox, once, with `emailctx auth` | Every teammate, on first connect from Claude |
| Status | Built | Not built yet (spec below) |
| In production | Replaced by email forwarding to `/inbound` (not built yet) | Still needed |

### 1. Inbox reading: set up `emailctx auth` (built)

1. In [Google Cloud Console](https://console.cloud.google.com), create a project. Any Google account can own it; it doesn't have to be the inbox's.
2. APIs & Services → Library → **Gmail API** → Enable.
3. OAuth consent screen (Google Auth Platform):
   - Audience: **External** and leave it in **Testing**, then add the inbox address under **Test users**. If the inbox is on Google Workspace and the project was created in that Workspace, choose **Internal** instead: no test-user list and no 7-day token expiry.
   - Branding: app name `emailctx`, plus a support email.
4. Clients → Create client → **Desktop app** → download the JSON and save it in the repo root as `client_secret.json`. It's gitignored; `google.client_secrets` in `config.yaml` sets the path, relative to the config file.
5. With Drive output off, remove `drive.file` from `google.scopes` in `config.yaml`, so the consent screen asks only for Gmail.
6. Run `emailctx auth`. A browser opens: sign in as the inbox, click through "Google hasn't verified this app" (expected in Testing), and allow read-only Gmail access. The token (with its refresh token) goes into the OS keyring; `emailctx auth --logout` removes it.
7. Run `emailctx serve --poll-minutes 1`. The server checks the inbox at start, then every minute, and logs each change.

Things to know:

- `emailctx auth` and `emailctx serve` must run on the same machine as the same OS user, because the token lives in that user's keyring.
- With External/Testing, Google expires the refresh token after 7 days. The poller then logs "not signed in"; run `emailctx auth` again.
- Test emails sent from a `@gmail.com` address can't be matched to a client by domain. List that exact address under the company's `contacts` in `registry.yaml`, or the emails go to the review queue.
- The first poll reads at most `gmail.backfill_max` (200) of the most recent matching emails. After that, only new mail.

### 2. MCP server user login (to build)

**Goal.** Claude Code, Claude Desktop or claude.ai connects to `emailctx serve`, the user signs in with Google once in a browser, and only accounts from allowed domains get in. Every write then records the user's email instead of `agent`.

**Why the server sits in the middle.** When an MCP client like Claude meets a server that requires login, it discovers the server's OAuth endpoints and registers itself automatically (dynamic client registration). Google doesn't support that, so Claude can't use Google directly. emailctx itself acts as the OAuth authorization server for Claude and hands only the sign-in step to Google:

```
Claude ──► emailctx /mcp: 401 + where to log in
Claude ──► emailctx /register: Claude registers itself (stored in SQLite)
Claude ──► emailctx /authorize ──► redirect to Google sign-in (openid email, state, PKCE)
                                        │ user picks their Google account
emailctx /oauth/google/callback ◄───────┘ code
   exchange code with Google, verify the ID token, check email_verified and the domain
   ──► issue an emailctx authorization code, redirect to Claude's callback
Claude ──► emailctx /token: emailctx access + refresh tokens
Claude ──► every /mcp call: Bearer token, checked on each request; the user is known
```

**Google Cloud setup** (same project as above):

1. Clients → Create client → **Web application**.
2. Authorized redirect URIs: `http://localhost:8000/oauth/google/callback` for local testing, and `https://<server-host>/oauth/google/callback` once deployed.
3. Scopes: only `openid` and `email` (basic scopes; no Google review).
4. While the project also holds the Gmail client in External/Testing, every teammate who signs in must be added as a test user. In production, forwarding replaces the Gmail client, so the project asks only for basic scopes and can be published to External with no review (the server's domain check keeps outsiders out), or set to Internal on Workspace.
5. Proposed environment variables: `EMAILCTX_GOOGLE_CLIENT_ID`, `EMAILCTX_GOOGLE_CLIENT_SECRET`, `EMAILCTX_ALLOWED_DOMAINS` (comma-separated, e.g. `yourcompany.com`), `EMAILCTX_PUBLIC_URL` (the issuer URL, e.g. `https://mcp.yourcompany.com`). Keep secrets in `.env` (gitignored), never in `config.yaml`.

**Where it plugs in:**

- `build_server()` in `src/emailctx/mcp_server.py` passes extra keyword arguments to `MCPServer(...)`. Give it `auth_server_provider=<GoogleProxyProvider>` and `auth=AuthSettings(issuer_url=..., resource_server_url=..., client_registration_options=ClientRegistrationOptions(enabled=True))` from `mcp.server.auth.settings`. The SDK then serves the discovery metadata, `/register`, `/authorize` and `/token` itself.
- Write one class implementing `OAuthAuthorizationServerProvider` from `mcp/server/auth/provider.py`:
  - `register_client` / `get_client`: save and look up Claude's registration.
  - `authorize`: remember the pending request (Claude's redirect URI, its PKCE challenge, its state) under a random key, and return a Google sign-in URL with that key as Google's `state`.
  - `load_authorization_code` / `exchange_authorization_code`: issue emailctx access and refresh tokens for a code from the callback.
  - `load_refresh_token` / `exchange_refresh_token`, `load_access_token`, `revoke_token`.
- Add the Google callback with `server.custom_route("/oauth/google/callback", methods=["GET"])`. Exchange Google's code at `https://oauth2.googleapis.com/token`, verify the ID token with `google.oauth2.id_token.verify_oauth2_token` (`google-auth` is already a dependency), require `email_verified`, check the email's domain against `EMAILCTX_ALLOWED_DOMAINS`, then create an emailctx authorization code and redirect to Claude's callback with Claude's original state.
- Store clients, pending authorizations, codes and tokens in SQLite: new tables in `src/emailctx/store/models.py`, so a restart doesn't log everyone out. Store token hashes, not raw tokens.
- Identity: `current_user_default()` in `mcp_server.py` already reads the request's token through `get_access_token()`. Put the user's email on the token as `subject` (or change that function), and every `set_item_status`, `log_question` and `record_answer` records the real user.
- CLI: add `--auth google` to `emailctx serve` in `src/emailctx/cli.py`. Keep no-auth as the default so the local demo works unchanged.

**Done when:**

- In Claude Code, `claude mcp add --transport http emailctx http://localhost:8000/mcp`, then `/mcp` → Authenticate, opens Google sign-in; after signing in, the tools work.
- An account outside the allowed domains is refused at the callback.
- After a status change, `list_items` shows `by <user email>`.
- Restarting the server doesn't sign anyone out.
- `emailctx serve` without `--auth` behaves as before, and `pytest` passes, with new tests for the provider using a fake Google token exchange (see `tests/test_tracker.py` for how the server is driven in tests).
