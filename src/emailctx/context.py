"""Find a company's requirements file by name and fetch it, for people and for Claude.

The lookup reads Drive directly (the files the app created), so it works from any
machine signed in with `emailctx auth`, even without the local database. Matching is
forgiving: case and extra spaces don't matter, ".md" is optional, and a partial name
works when it points at exactly one company ("acme" -> "Acme Corp").
"""

from __future__ import annotations

from dataclasses import dataclass

from emailctx.drive import P_KEY, DriveStore


@dataclass(frozen=True)
class CompanyFile:
    company_id: str
    name: str  # display name, from the file name without ".md"
    file_id: str
    modified: str | None = None
    link: str | None = None


class CompanyNotFound(LookupError):
    pass


def _norm(s: str) -> str:
    s = " ".join(s.casefold().split())
    return s[:-3].rstrip() if s.endswith(".md") else s


def list_company_files(store: DriveStore) -> list[CompanyFile]:
    files = [
        CompanyFile(
            company_id=(f.get("appProperties") or {}).get(P_KEY, ""),
            name=f["name"][:-3] if f["name"].lower().endswith(".md") else f["name"],
            file_id=f["id"],
            modified=f.get("modifiedTime"),
            link=f.get("webViewLink"),
        )
        for f in store.list_contexts()
    ]
    return sorted(files, key=lambda c: c.name.casefold())


def find_company(files: list[CompanyFile], query: str) -> CompanyFile:
    q = _norm(query)
    if not q:
        raise CompanyNotFound("give a company name")
    for match in (
        lambda c: q in (_norm(c.name), _norm(c.company_id)),  # exact name or id
        lambda c: _norm(c.name).startswith(q) or _norm(c.company_id).startswith(q),
        lambda c: q in _norm(c.name) or q in _norm(c.company_id),
    ):
        hits = [c for c in files if match(c)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(c.name for c in hits)
            raise CompanyNotFound(f"{query!r} matches several companies: {names}. Be more specific.")
    known = ", ".join(c.name for c in files) or "none yet"
    raise CompanyNotFound(f"no company file matches {query!r}. Companies with a file: {known}.")


def fetch_context(store: DriveStore, query: str) -> tuple[CompanyFile, str]:
    """(the company's file, its markdown) for a name, id or unambiguous part of a name."""
    company = find_company(list_company_files(store), query)
    return company, store.read(company.file_id).decode("utf-8")
