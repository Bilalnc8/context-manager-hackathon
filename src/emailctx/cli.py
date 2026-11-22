"""emailctx command line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from emailctx.config import Settings, load_settings
from emailctx.store.db import make_engine, make_sessionmaker

app = typer.Typer(help="Route emails to projects and maintain one context.md per company.")
review_app = typer.Typer(help="Emails waiting for a person to pick their project.")
registry_app = typer.Typer(help="Companies, domains, contacts and projects.")
conflicts_app = typer.Typer(help='Requirement changes under "Needs confirmation".')
app.add_typer(review_app, name="review")
app.add_typer(registry_app, name="registry")
app.add_typer(conflicts_app, name="conflicts")

ConfigOpt = Annotated[
    Optional[Path],
    typer.Option("--config", help="Config file (default: $EMAILCTX_CONFIG or ./config.yaml)."),
]


def _credentials(settings: Settings):
    from emailctx.auth import KeyringTokenStore, load_credentials

    return load_credentials(settings, KeyringTokenStore())


def _drive_store(settings: Settings):
    """Signed-in DriveStore. Raises AuthError with what to do next."""
    from emailctx.auth import drive_service
    from emailctx.drive import DriveStore

    return DriveStore(drive_service(_credentials(settings)), settings.output.drive.root_folder)


def _runtime(settings: Settings, *, llm: bool = True):
    """Sessions, the LLM client and, when Drive output is on, the signed-in Drive store."""
    from emailctx.auth import AuthError
    from emailctx.ingest import Runtime
    from emailctx.llm import AnthropicLLM

    sessions = make_sessionmaker(make_engine(settings.db_path))
    drive = None
    if settings.output.drive.enabled:
        try:
            drive = _drive_store(settings)
        except AuthError as e:
            typer.secho(f"error: {e} (Drive output is on)", fg="red")
            raise typer.Exit(2) from e
    return Runtime(settings, sessions, AnthropicLLM(settings) if llm else None, drive=drive)  # type: ignore[arg-type]


def _print_result(result) -> None:
    typer.echo(f"msg_id:  {result.msg_id}")
    if result.route is not None:
        r = result.route
        where = r.project_id or (", ".join(r.candidates) if r.candidates else "-")
        typer.echo(f"route:   {r.status} via {r.step} -> {where}" + (f" ({r.reason})" if r.reason else ""))
    typer.echo(f"outcome: {result.outcome}")
    if result.review_id is not None:
        typer.secho(f"queued for review as item {result.review_id}", fg="yellow")
    for c in result.applied:
        typer.secho(f"  applied  {c.op:<6} {c.req_id}: {c.new_text or c.old_text}", fg="green")
    for q in result.answered:
        typer.secho(f"  answered {q}", fg="green")
    for o in result.dropped:
        typer.echo(f"  dropped  {o.op.op:<6} {o.reason}")
    for o in result.rejected:
        typer.secho(f"  rejected {o.op.op:<6} {o.reason}", fg="yellow")
    if result.outcome == "gated_out" and result.gate_reason:
        typer.echo(f"  gate:    {result.gate_reason}")
    for s in result.skipped:
        typer.echo(f'  skipped  "{s.quote}" ({s.reason})')
    for s in result.unaccounted:
        typer.secho(f'  NOT CAPTURED "{s}"', fg="red")
    for p in result.published:
        typer.echo(f"{'wrote' if p.written else 'unchanged'} {p.destination}")


def _gmail_service(settings: Settings):
    """Signed-in Gmail API client. Raises AuthError with what to do next."""
    from emailctx.auth import gmail_service

    return gmail_service(_credentials(settings))


def _print_bootstrap(b) -> None:
    processed = sum(1 for _, r in b.results if r.status == "processed")
    changes = sum(len(r.applied) for _, r in b.results)
    written = [p.destination for p in b.published if p.written]
    typer.secho(
        f"built {b.company_id}: {b.candidates} emails found, {processed} processed, "
        f"{changes} requirement changes, {b.known} already known, {b.other_company} for other companies",
        fg="green",
    )
    for d in written:
        typer.echo(f"  wrote {d}")


@app.command("auth")
def auth_cmd(
    logout: Annotated[bool, typer.Option("--logout", help="Forget the stored Google token.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Sign in to Google (read-only) in the browser; the token goes to the OS keyring."""
    from emailctx.auth import AuthError, KeyringTokenStore, run_auth_flow

    settings = load_settings(config)
    store = KeyringTokenStore()
    if logout:
        store.clear()
        typer.echo("signed out: token removed from the keyring")
        return
    try:
        run_auth_flow(settings, store)
        profile = _gmail_service(settings).users().getProfile(userId="me").execute()
    except AuthError as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e
    typer.echo(f"signed in as {profile['emailAddress']} (scopes: {', '.join(settings.google.scopes)})")


@app.command("sync")
def sync_cmd(
    backfill_max: Annotated[
        Optional[int],
        typer.Option("--backfill-max", help="Override gmail.backfill_max for a first sync."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Fetch new Gmail messages matching gmail.query and check each for requirement changes."""
    from emailctx.auth import AuthError
    from emailctx.sources.gmail_sync import GmailSync, SyncError

    settings = load_settings(config)
    rt = _runtime(settings)
    try:
        service = _gmail_service(settings)
    except AuthError as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e

    def show(gmail_id: str, result) -> None:
        r = result.route
        where = (r.project_id or "review") if r else "-"
        changes = ", ".join(f"{c.req_id} {c.op}" for c in result.applied)
        line = f"  {gmail_id}  {result.outcome:<18} {where}"
        typer.echo(line + (f"  [{changes}]" if changes else ""))

    sync = GmailSync(service, rt, backfill_max=backfill_max, on_result=show)
    try:
        report = sync.run()
    except SyncError as e:
        for b in e.report.bootstraps:
            _print_bootstrap(b)
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(1) from e
    for b in report.bootstraps:
        _print_bootstrap(b)
    typer.echo(
        f"{report.account}: {report.mode}, {report.candidates} candidates, "
        f"{len(report.results)} processed, {report.known} already known, "
        f"{report.not_matching} outside the query, {report.skipped_labels} drafts/spam/trash"
    )


@app.command("context")
def context_cmd(
    company: Annotated[
        Optional[str], typer.Argument(help="Company name or id (a unique part of the name works).")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Print a company's requirements file from Google Drive (no name: list companies)."""
    from emailctx.auth import AuthError
    from emailctx.context import CompanyNotFound, fetch_context, list_company_files

    settings = load_settings(config)
    try:
        store = _drive_store(settings)
        if company is None:
            files = list_company_files(store)
            if not files:
                typer.echo("no company files yet: run `emailctx sync`")
            for c in files:
                typer.echo(c.name + (f"  (updated {c.modified[:10]})" if c.modified else ""))
            return
        _, text = fetch_context(store, company)
    except AuthError as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(2) from e
    except CompanyNotFound as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(text, nl=False)


def _server_runtime(settings: Settings):
    """Runtime for the MCP server. Built on the first tool call, never exits the process."""
    from emailctx.auth import AuthError
    from emailctx.ingest import Runtime
    from emailctx.llm import AnthropicLLM

    sessions = make_sessionmaker(make_engine(settings.db_path))
    drive = None
    if settings.output.drive.enabled:
        try:
            drive = _drive_store(settings)
        except AuthError as e:
            raise RuntimeError(
                f"{e}. Drive output is on in the server's config: sign in, or set output.drive.enabled: false"
            ) from e
    return Runtime(settings, sessions, AnthropicLLM(settings), drive=drive)


@app.command("mcp")
def mcp_cmd(config: ConfigOpt = None) -> None:
    """Run the MCP server over stdio, for one machine.

    Register once:  claude mcp add emailctx --scope user -e EMAILCTX_CONFIG=<config.yaml> -- <emailctx> mcp
    """
    from emailctx.mcp_server import run

    settings = load_settings(config)
    run(lambda: _server_runtime(settings))


@app.command("serve")
def serve_cmd(
    host: Annotated[str, typer.Option(help="Interface to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    allowed_host: Annotated[
        Optional[list[str]],
        typer.Option(
            "--allowed-host",
            help="Extra Host header to accept, e.g. a tunnel's my-team.ngrok.app (repeatable).",
        ),
    ] = None,
    poll_minutes: Annotated[
        Optional[float],
        typer.Option(
            "--poll-minutes",
            help="Check Gmail for new client mail every N minutes (default: gmail.poll_minutes; 0 = off). "
            "Needs `emailctx auth` on this machine, signed in as the shared inbox.",
        ),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Run the MCP server over HTTP so the whole team's Claude sessions can connect.

    Claude Code:  claude mcp add --transport http emailctx http://localhost:8000/mcp

    One process owns the database: tool calls and the Gmail poller share one runtime and
    one write lock, so writes take turns and the published files stay current.
    """
    import threading

    from mcp.server.transport_security import TransportSecuritySettings

    from emailctx.mcp_server import run

    settings = load_settings(config)
    made: dict = {}
    made_lock = threading.Lock()

    def runtime():
        with made_lock:  # the poller and the first tool call may race to build it
            if "rt" not in made:
                made["rt"] = _server_runtime(settings)
            return made["rt"]

    minutes = settings.gmail.poll_minutes if poll_minutes is None else poll_minutes
    if minutes and minutes > 0:
        _start_gmail_poller(settings, runtime, minutes)
    local = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    extra = list(allowed_host or [])
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=local + extra,
        allowed_origins=[f"http://{h}" for h in local] + [f"https://{h}" for h in extra],
    )
    typer.echo(f"emailctx MCP server on http://{host}:{port}/mcp (database {settings.db_path})", err=True)
    run(runtime, transport="http", host=host, port=port, transport_security=security)


def _start_gmail_poller(settings: Settings, runtime, minutes: float) -> None:
    """Background thread: run a Gmail sync now, then every `minutes`. Errors are logged
    and retried next round; the server keeps serving either way."""
    import threading
    import time

    from emailctx.auth import AuthError
    from emailctx.sources.gmail_sync import GmailSync, SyncError

    def log(msg: str) -> None:
        typer.echo(f"[gmail] {msg}", err=True)

    def loop() -> None:
        while True:
            try:
                report = GmailSync(_gmail_service(settings), runtime()).run()
                changed = [
                    f"{r.company_id}: " + ", ".join(
                        [f"{c.req_id} {c.op}" for c in r.applied] + [f"{q} answered" for q in r.answered]
                    )
                    for _, r in report.results if r.applied or r.answered
                ]
                log(f"{report.account}: {report.mode}, {len(report.results)} new, "
                    f"{len(changed)} with changes" + ("".join(f"\n  {c}" for c in changed)))
            except AuthError as e:
                log(f"{e} (sign in as the shared inbox); retrying in {minutes:g} min")
            except SyncError as e:
                log(f"sync stopped, will resume from the same point: {e}")
            except Exception as e:  # keep the server up; the next round retries
                log(f"sync failed: {e!r}")
            time.sleep(minutes * 60)

    threading.Thread(target=loop, name="gmail-poller", daemon=True).start()
    typer.echo(f"[gmail] checking for new client mail every {minutes:g} min", err=True)


@app.command("bootstrap")
def bootstrap_cmd(
    company: Annotated[
        Optional[str], typer.Option("--company", "-c", help="Only this company (runs even if its .md exists).")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Build the .md for every company that doesn't have one yet, from recent Gmail mail.

    `emailctx sync` does this automatically; this command runs it on its own.
    """
    from emailctx.auth import AuthError
    from emailctx.bootstrap import bootstrap_company, companies_missing_context

    settings = load_settings(config)
    rt = _runtime(settings)
    try:
        service = _gmail_service(settings)
    except AuthError as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e
    if company:
        targets = [company]
    elif rt.drive is not None:
        with rt.sessions() as s:
            targets = companies_missing_context(s, rt.drive.has_context)
    else:
        typer.secho("error: without Drive output, name the company (--company)", fg="red")
        raise typer.Exit(2)
    if not targets:
        typer.echo("every company already has its .md")
        return
    for cid in targets:
        _print_bootstrap(bootstrap_company(service, rt, cid))


@app.command("ingest-file")
def ingest_file_cmd(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="An .eml file.")],
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Skip routing and use this project."),
    ] = None,
    company: Annotated[
        Optional[str],
        typer.Option(
            "--company", "-c",
            help="Skip routing and use this company's project (created if needed).",
        ),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Route one .eml file and check it for requirement changes."""
    from emailctx.ingest import IngestError, ingest_file
    from emailctx.llm import LLMError

    rt = _runtime(load_settings(config))
    try:
        result = ingest_file(
            path, project, company_id=company, settings=rt.settings, sessions=rt.sessions,
            llm=rt.llm, drive=rt.drive,
        )
    except IngestError as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e
    except LLMError as e:
        typer.secho(f"error: {e} (message not marked processed; safe to retry)", fg="red")
        raise typer.Exit(1) from e
    _print_result(result)


@app.command("render")
def render_cmd(
    company: Annotated[str, typer.Option("--company", "-c", help="Company id.")],
    config: ConfigOpt = None,
) -> None:
    """Re-render a company's context.md and publish it wherever it changed."""
    from emailctx.render.render import render_and_publish

    settings = load_settings(config)
    rt = _runtime(settings, llm=False)
    with rt.sessions() as session:
        try:
            results = render_and_publish(
                session, company, settings, rt.targets(), message=f"{company}: re-render"
            )
        except KeyError as e:
            typer.secho(f"error: {e}", fg="red")
            raise typer.Exit(1) from e
    for p in results:
        typer.echo(f"{'wrote' if p.written else 'unchanged'} {p.destination}")


# --- registry ---------------------------------------------------------------------------


@registry_app.command("load")
def registry_load_cmd(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="registry.yaml")],
    config: ConfigOpt = None,
) -> None:
    """Upsert companies, domains, contacts and projects from a YAML file."""
    from emailctx.registry import load_registry, read_registry

    settings = load_settings(config)
    sessions = make_sessionmaker(make_engine(settings.db_path))
    with sessions() as session:
        try:
            counts = load_registry(session, read_registry(path))
        except ValueError as e:
            typer.secho(f"error: {e}", fg="red")
            raise typer.Exit(2) from e
        session.commit()
    typer.echo("added/updated: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


# --- review -----------------------------------------------------------------------------


@review_app.command("list")
def review_list_cmd(config: ConfigOpt = None) -> None:
    """Show open review items."""
    from emailctx.review import list_open

    settings = load_settings(config)
    sessions = make_sessionmaker(make_engine(settings.db_path))
    with sessions() as session:
        items = list_open(session)
    if not items:
        typer.echo("review queue is empty")
        return
    for it in items:
        date = it.date.strftime("%Y-%m-%d") if it.date else "undated"
        typer.secho(f"[{it.id}] {date}  {it.subject or '(no subject)'}", bold=True)
        typer.echo(f"     {it.kind}: {it.reason}")
        if it.candidates:
            typer.echo(f"     candidates: {', '.join(it.candidates)}")
        if it.pending_companies:
            typer.echo(f"     pending companies: {', '.join(it.pending_companies)}")


@review_app.command("assign")
def review_assign_cmd(
    item: Annotated[int, typer.Argument(help="Review item id.")],
    project: Annotated[
        Optional[str], typer.Option("--project", "-p", help="Project to assign.")
    ] = None,
    company: Annotated[
        Optional[str],
        typer.Option("--company", "-c", help="Company; alone it means the company's one project."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Assign an item to a company's project, learn rules from it, and process the email."""
    if project is None and company is None:
        typer.secho("error: give --company (or --project)", fg="red")
        raise typer.Exit(2)
    from emailctx.auth import AuthError
    from emailctx.llm import LLMError
    from emailctx.review import ReviewError, assign, make_reader
    from emailctx.sources.gmail_sync import fetch_raw

    settings = load_settings(config)
    rt = _runtime(settings)

    def read_gmail(gmail_id: str) -> bytes:  # signs in only if the item came from Gmail
        try:
            return fetch_raw(_gmail_service(settings), gmail_id)[0]
        except AuthError as e:
            raise ReviewError(str(e)) from e

    try:
        result = assign(item, project, rt, company_id=company, reader=make_reader(gmail=read_gmail))
    except ReviewError as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e
    except LLMError as e:
        typer.secho(f"error: {e} (item left open; safe to retry)", fg="red")
        raise typer.Exit(1) from e
    _print_result(result)


@review_app.command("dismiss")
def review_dismiss_cmd(
    item: Annotated[int, typer.Argument(help="Review item id.")],
    ignore_domains: Annotated[
        bool, typer.Option("--ignore-domains", help="Stop routing this item's unknown domains to review.")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Mark an item as belonging to no project."""
    from emailctx.ingest import Runtime
    from emailctx.review import ReviewError, dismiss

    settings = load_settings(config)
    rt = Runtime(settings, make_sessionmaker(make_engine(settings.db_path)), llm=None)  # type: ignore[arg-type]
    try:
        dismiss(item, rt, ignore_domains=ignore_domains)
    except ReviewError as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e
    typer.echo(f"dismissed {item}")


# --- conflicts --------------------------------------------------------------------------


@conflicts_app.command("list")
def conflicts_list_cmd(
    company: Annotated[Optional[str], typer.Option("--company", "-c", help="Only this company.")] = None,
    config: ConfigOpt = None,
) -> None:
    """Show open conflicts."""
    from emailctx.conflicts import list_open

    settings = load_settings(config)
    sessions = make_sessionmaker(make_engine(settings.db_path))
    with sessions() as session:
        items = list_open(session, company)
    if not items:
        typer.echo("nothing needs confirmation")
        return
    for c in items:
        typer.secho(f"[C{c.conflict_id}] {c.project_id} {c.req_id} ({c.kind})", bold=True)
        typer.echo(f"     kept:  {c.kept_text}  [{c.kept_source_id}]")
        typer.echo(f"     other: {c.other_text if c.other_text is not None else '(remove it)'}  [{c.other_source_id}]")


@conflicts_app.command("confirm")
def conflicts_confirm_cmd(
    conflict: Annotated[str, typer.Argument(help="Conflict id, e.g. C3 or 3.")],
    use_other: Annotated[
        bool, typer.Option("--use-other", help="Switch to the other email's version instead.")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Accept the current version (or switch with --use-other) and clear the item."""
    from emailctx.conflicts import ConflictError, confirm

    rt = _runtime(load_settings(config), llm=False)
    try:
        results = confirm(int(conflict.upper().lstrip("C")), rt, use_other=use_other)
    except (ConflictError, ValueError) as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e
    for p in results:
        typer.echo(f"{'wrote' if p.written else 'unchanged'} {p.destination}")


# --- sample project and eval ------------------------------------------------------------

eval_app = typer.Typer(help="Score the pipeline on the company's sample project.")
app.add_typer(eval_app, name="eval")


def _sample_line(item, result) -> str:
    changes = len(result.applied) + len(result.answered)
    extra = f", {len(result.unaccounted)} NOT CAPTURED" if result.unaccounted else ""
    where = result.company_id or "-"
    return f"{item.source_id:<12} {where:<8} {result.outcome:<17} {changes} changes{extra}"


@app.command("load-sample")
def load_sample_cmd(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Sample folder.")] = Path("dummy_data"),
    config: ConfigOpt = None,
) -> None:
    """Load the sample project (registry, emails, meeting notes) into the configured store,
    in date order. Running it again changes nothing."""
    from emailctx.llm import LLMError
    from emailctx.sample import load_sample, read_sample

    settings = load_settings(config)
    rt = _runtime(settings)
    try:
        load_sample(
            read_sample(folder), rt, settings.db_path.parent / "sample-eml",
            on_result=lambda i, r: typer.echo(_sample_line(i, r)),
        )
    except LLMError as e:
        typer.secho(f"error: {e} (inputs so far are saved; rerun to continue)", fg="red")
        raise typer.Exit(1) from e


@eval_app.command("run")
def eval_run_cmd(
    sample: Annotated[Path, typer.Option("--sample", exists=True, file_okay=False)] = Path("dummy_data"),
    facts: Annotated[Path, typer.Option("--facts", exists=True, file_okay=False)] = Path("eval/facts"),
    workdir: Annotated[Path, typer.Option("--workdir", help="Fresh store, generated files and report.md.")] = Path("eval/run"),
    config: ConfigOpt = None,
) -> None:
    """Process the sample in a fresh store with the configured models, then score each
    generated file against the expected facts. Calls the Anthropic API (costs money)."""
    from emailctx.evaluate import EvalError, run_eval
    from emailctx.llm import AnthropicLLM, LLMError

    settings = load_settings(config)
    try:
        report = run_eval(
            sample, facts, workdir, settings, AnthropicLLM(settings),
            on_result=lambda i, r: typer.echo(_sample_line(i, r)),
        )
    except (EvalError, LLMError) as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(1) from e
    present, total = report.score()
    typer.echo("")
    typer.echo(report.to_markdown())
    typer.secho(f"{present}/{total} facts present; report: {workdir / 'report.md'}", fg="green")


@eval_app.command("draft-facts")
def eval_draft_facts_cmd(
    expected: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Expected context .md")],
    project: Annotated[str, typer.Option("--project", "-p", help="Project id for the facts file.")],
    config: ConfigOpt = None,
) -> None:
    """Draft a facts checklist (YAML on stdout) from an expected-outcome file, to review
    and save under eval/facts/."""
    import yaml

    from emailctx.evaluate import draft_facts
    from emailctx.llm import AnthropicLLM

    settings = load_settings(config)
    drafted = draft_facts(AnthropicLLM(settings), expected.read_text(encoding="utf-8"))
    data = {"project": project, "expected": expected.as_posix(),
            "facts": [f.model_dump() for f in drafted.facts]}
    typer.echo(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


if __name__ == "__main__":  # pragma: no cover
    app()
