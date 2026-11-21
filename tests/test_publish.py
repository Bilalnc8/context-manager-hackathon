from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from conftest import FakeS3

from emailctx.config import OutputSettings, S3Settings, Settings
from emailctx.llm import RecordedLLM
from emailctx.render.publish import LocalPublisher, S3Publisher, publishers_from_settings


def test_local_publisher_writes_only_on_change(tmp_path):
    pub = LocalPublisher(tmp_path / "companies")
    first = pub.publish("acme", "a\n")
    assert first.written and first.destination == str(tmp_path / "companies" / "acme" / "context.md")
    mtime = (tmp_path / "companies" / "acme" / "context.md").stat().st_mtime_ns
    assert not pub.publish("acme", "a\n").written
    assert (tmp_path / "companies" / "acme" / "context.md").stat().st_mtime_ns == mtime
    assert pub.publish("acme", "b\n").written


def test_s3_publisher_writes_only_on_change():
    s3 = FakeS3()
    pub = S3Publisher(S3Settings(bucket="ctx-bucket", prefix="context/"), client=s3)

    first = pub.publish("acme", "# Acme\n")
    assert first.written and first.destination == "s3://ctx-bucket/context/acme/context.md"
    obj = s3.objects[("ctx-bucket", "context/acme/context.md")]
    assert obj["Body"] == b"# Acme\n"
    assert obj["ContentType"] == "text/markdown; charset=utf-8"
    assert obj["Metadata"] == {"sha256": first.sha256}

    assert not pub.publish("acme", "# Acme\n").written
    assert s3.puts == 1  # unchanged render: no new object version
    assert pub.publish("acme", "# Acme v2\n").written
    assert s3.puts == 2


def test_s3_publisher_surfaces_errors_other_than_not_found():
    class Denied(FakeS3):
        def head_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

    pub = S3Publisher(S3Settings(bucket="b"), client=Denied())
    with pytest.raises(ClientError):
        pub.publish("acme", "x\n")


def test_s3_disabled_unless_bucket_configured(tmp_path):
    local_only = Settings(output=OutputSettings(local_dir=tmp_path))
    assert [type(p) for p in publishers_from_settings(local_only)] == [LocalPublisher]
    both = Settings(output=OutputSettings(local_dir=tmp_path, s3=S3Settings(bucket="b")))
    assert [type(p) for p in publishers_from_settings(both, s3_client=FakeS3())] == [
        LocalPublisher,
        S3Publisher,
    ]
    s3_only = Settings(output=OutputSettings(local_dir=None, s3=S3Settings(bucket="b")))
    assert [type(p) for p in publishers_from_settings(s3_only, s3_client=FakeS3())] == [S3Publisher]


def test_pipeline_publishes_to_s3_only_when_md_changes(env):
    """End to end: a real change uploads once; a reworded email uploads nothing."""
    s3 = FakeS3()
    env.settings = env.settings.model_copy(
        update={"output": OutputSettings(local_dir=None, s3=S3Settings(bucket="b"))}
    )

    from emailctx.ingest import ingest_file
    from conftest import EML, LLM

    def run(eml, llm):
        return ingest_file(EML / f"{eml}.eml", "atlas", company_id="acme", settings=env.settings,
                           sessions=env.sessions, llm=RecordedLLM.from_file(LLM / f"{llm}.json"),
                           publishers=publishers_from_settings(env.settings, s3_client=s3))

    run("atlas_01_initial", "atlas_01_initial")
    assert s3.puts == 1
    run("atlas_03_reworded", "atlas_03_reworded_modify")
    assert s3.puts == 1  # nothing applied: no render, no upload
    result = run("atlas_04_real_change", "atlas_04_real_change")
    assert s3.puts == 2
    assert [p.destination for p in result.published] == ["s3://b/acme/context.md"]
    body = s3.objects[("b", "acme/context.md")]["Body"].decode()
    assert "The export must support CSV and XLSX." in body
