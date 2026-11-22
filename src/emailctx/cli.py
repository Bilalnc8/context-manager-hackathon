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
    for o in result.dropped:
        typer.echo(f"  dropped  {o.op.op:<6} {o.reason}")
    for o in result.rejected:
        typer.secho(f"  rejected {o.op.op:<6} {o.reason}", fg="yellow")
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


@app.command("mcp")
def mcp_cmd(config: ConfigOpt = None) -> None:
    """Run the MCP server (stdio) that lets Claude sessions fetch company files.

    Register once:  claude mcp add emailctx --scope user -e EMAILCTX_CONFIG=<config.yaml> -- <emailctx> mcp
    """
    from emailctx.mcp_server import run

    settings = load_settings(config)
    run(lambda: _drive_store(settings))


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


if __name__ == "__main__":  # pragma: no cover
    app()
