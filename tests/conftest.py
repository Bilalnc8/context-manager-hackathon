from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from emailctx.config import OutputSettings, Settings
from emailctx.ingest import IngestResult, ingest_file
from emailctx.llm import RecordedLLM
from emailctx.store.db import make_engine, make_sessionmaker
from emailctx.store.models import (
    ChangeLog,
    Message,
    ProcessedMessage,
    Requirement,
    Segment,
    ThreadParagraph,
)

FIXTURES = Path(__file__).parent / "fixtures"
EML = FIXTURES / "eml"
LLM = FIXTURES / "llm"


class FakeS3:
    """Just enough of the boto3 S3 client for S3Publisher."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.puts = 0

    def head_object(self, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"Metadata": dict(self.objects[(Bucket, Key)]["Metadata"])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str, Metadata: dict) -> dict:
        self.objects[(Bucket, Key)] = {"Body": Body, "ContentType": ContentType, "Metadata": Metadata}
        self.puts += 1
        return {}


@dataclass
class Env:
    settings: Settings
    sessions: sessionmaker[Session]

    def ingest(
        self,
        eml: str,
        llm: str | RecordedLLM | None = None,
        project: str = "atlas",
        company: str | None = "acme",
    ) -> tuple[IngestResult, RecordedLLM]:
        """Ingest tests/fixtures/eml/<eml>.eml with recorded responses from llm/<llm>.json.

        llm=None means no LLM call is expected; any call fails the test.
        """
        if isinstance(llm, RecordedLLM):
            recorded = llm
        elif llm is None:
            recorded = RecordedLLM({})
        else:
            recorded = RecordedLLM.from_file(LLM / f"{llm}.json")
        result = ingest_file(
            EML / f"{eml}.eml",
            project,
            company_id=company,
            settings=self.settings,
            sessions=self.sessions,
            llm=recorded,
        )
        return result, recorded

    def md_path(self, company: str = "acme") -> Path:
        assert self.settings.output.local_dir is not None
        return self.settings.output.local_dir / company / "context.md"

    def md_bytes(self, company: str = "acme") -> bytes:
        return self.md_path(company).read_bytes()

    def snapshot(self) -> dict:
        """Everything the pipeline can change, for before/after comparisons."""
        with self.sessions() as s:
            counts = {
                m.__tablename__: s.scalar(select(func.count()).select_from(m))
                for m in (Message, Segment, Requirement, ChangeLog, ProcessedMessage, ThreadParagraph)
            }
            reqs = [
                (r.project_id, r.req_id, r.text, r.category, r.status, r.version, tuple(r.source_ids))
                for r in s.scalars(select(Requirement).order_by(Requirement.project_id, Requirement.seq))
            ]
        return {"counts": counts, "requirements": reqs}

    def requirements(self, project: str = "atlas") -> list[Requirement]:
        with self.sessions() as s:
            return list(
                s.scalars(
                    select(Requirement)
                    .where(Requirement.project_id == project)
                    .order_by(Requirement.seq)
                )
            )

    def change_log(self, project: str = "atlas") -> list[ChangeLog]:
        with self.sessions() as s:
            return list(
                s.scalars(
                    select(ChangeLog)
                    .where(ChangeLog.project_id == project)
                    .order_by(ChangeLog.change_id)
                )
            )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    settings = Settings(
        db_path=tmp_path / "test.db",
        output=OutputSettings(local_dir=tmp_path / "companies"),
    )
    return Env(settings, make_sessionmaker(make_engine(settings.db_path)))


@pytest.fixture
def seeded(env: Env) -> Env:
    """Company acme, project atlas, with REQ-001 (CSV export) and REQ-002 (2s load time)."""
    result, llm = env.ingest("atlas_01_initial", "atlas_01_initial")
    assert result.outcome == "applied", result
    assert llm.remaining() == {}
    return env
