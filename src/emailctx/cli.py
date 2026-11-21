"""emailctx command line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from emailctx.config import load_settings
from emailctx.store.db import make_engine, make_sessionmaker

app = typer.Typer(help="Route emails to projects and maintain one context.md per company.")

ConfigOpt = Annotated[
    Optional[Path],
    typer.Option("--config", help="Config file (default: $EMAILCTX_CONFIG or ./config.yaml)."),
]


def _print_published(results) -> None:
    for r in results:
        typer.echo(f"{'wrote' if r.written else 'unchanged'} {r.destination}")


@app.command("ingest-file")
def ingest_file_cmd(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="An .eml file.")],
    project: Annotated[str, typer.Option("--project", "-p", help="Target project id.")],
    company: Annotated[
        Optional[str],
        typer.Option("--company", "-c", help="Company id. Required the first time a project is seen."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Check one .eml file for requirement changes to one project."""
    from emailctx.ingest import IngestError, ingest_file
    from emailctx.llm import AnthropicLLM, LLMError

    settings = load_settings(config)
    sessions = make_sessionmaker(make_engine(settings.db_path))
    try:
        result = ingest_file(
            path,
            project,
            company_id=company,
            settings=settings,
            sessions=sessions,
            llm=AnthropicLLM(settings),
        )
    except IngestError as e:
        typer.secho(f"error: {e}", fg="red")
        raise typer.Exit(2) from e
    except LLMError as e:
        typer.secho(f"error: {e} (message not marked processed; safe to retry)", fg="red")
        raise typer.Exit(1) from e

    typer.echo(f"msg_id:  {result.msg_id}")
    typer.echo(f"outcome: {result.outcome}")
    for c in result.applied:
        typer.secho(f"  applied  {c.op:<6} {c.req_id}: {c.new_text or c.old_text}", fg="green")
    for o in result.dropped:
        typer.echo(f"  dropped  {o.op.op:<6} {o.reason}")
    for o in result.rejected:
        typer.secho(f"  rejected {o.op.op:<6} {o.reason}", fg="yellow")
    _print_published(result.published)


@app.command("render")
def render_cmd(
    company: Annotated[str, typer.Option("--company", "-c", help="Company id.")],
    config: ConfigOpt = None,
) -> None:
    """Re-render a company's context.md and publish it wherever it changed."""
    from emailctx.render.publish import publishers_from_settings
    from emailctx.render.render import render_and_publish

    settings = load_settings(config)
    sessions = make_sessionmaker(make_engine(settings.db_path))
    with sessions() as session:
        try:
            results = render_and_publish(
                session, company, settings, publishers_from_settings(settings)
            )
        except KeyError as e:
            typer.secho(f"error: {e}", fg="red")
            raise typer.Exit(1) from e
    _print_published(results)


if __name__ == "__main__":  # pragma: no cover
    app()
