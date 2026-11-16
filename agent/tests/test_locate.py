"""ONE resolver for "which asset of this Project is at this location" (ingestion.locate). The UI's resolve_locations,
lineage capture and generated-media registration all ask it, so a copy of an M2C output resolves to the original's
asset on every path (it used to on the UI path only: trimming a TTS chunk worked while its "made from" link dropped)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingestion import locate  # noqa: E402
from ingestion.lineage_capture import LineageCapture  # noqa: E402

PID = "p" + "0" * 32
OURS = "ours"
ASSET = "audio:11111111-1111-4111-8111-111111111111"
ORIGIN = ("m2c-proxy", "micro-workflow/tts-on-aws/output/ca3d/ca3d_0_0_c0.wav")
COPY = f"projects/{PID}/sessions/s1/output/ca3d/ca3d_0_0_c0.wav"


class _Store:
    def __init__(self, guards=None, jobs=None, rows=None):
        self.guards, self.jobs, self.rows = guards or {}, jobs or {}, rows or {}

    def asset_id_for_location(self, pid, bucket, key):
        return self.guards.get((bucket, key))

    def job_for_execution(self, pid, exe):
        return self.jobs.get(exe)

    def get_asset(self, pid, aid):
        return self.rows.get(aid)


class _S3:
    def __init__(self, meta):
        self.meta = meta

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.meta:
            raise Exception("404")
        return {"Metadata": self.meta[(Bucket, Key)]}


class _Edges:
    def __init__(self, aliases=None):
        self.aliases = aliases or {}

    def alias_of(self, pid, bucket, key):
        return self.aliases.get((bucket, key))


def find(store, bucket, key, **kw):
    return locate.asset_for_location(store, PID, bucket, key, ingest_bucket=OURS, **kw)


def test_the_chain_in_order():
    store = _Store(guards={ORIGIN: ASSET, (OURS, "projects/x/registered.mp4"): "video:r"})
    assert find(store, OURS, f"projects/{PID}/{ASSET}/a.wav") == ASSET                          # 2. an asset key
    assert find(store, OURS, "projects/x/registered.mp4") == "video:r"                           # 3. SRC guard
    assert find(store, OURS, COPY, s3=_S3({(OURS, COPY): {"origin": f"s3://{ORIGIN[0]}/{ORIGIN[1]}"}})) == ASSET  # 4.
    assert find(store, OURS, "artifacts/x/dup.wav", edges=_Edges({(OURS, "artifacts/x/dup.wav"): ASSET})) == ASSET  # 5.
    assert find(store, OURS, f"artifacts/m2c/{ORIGIN[0]}/{ORIGIN[1]}") == ASSET                 # 1. an M2C reference


def test_each_step_runs_only_if_the_caller_can_and_nothing_ever_raises():
    store = _Store(guards={ORIGIN: ASSET})
    assert find(store, OURS, COPY) is None                                   # no S3 client: the origin step is skipped
    assert find(store, OURS, COPY, s3=_S3({})) is None                      # not there / unreadable
    assert find(store, OURS, COPY, s3=_S3({(OURS, COPY): {}})) is None      # a copy without an origin
    assert find(store, "elsewhere", "projects/x/k", s3=_S3({})) is None     # not ours: no project key, no origin
    assert locate.asset_for_location(store, PID, "", "k") is None


def test_legacy_a_pre_origin_copy_found_through_its_job_record():
    exe = "f29bd1dc-4974-5fe6-accf-b596b66f35ca"
    store = _Store(guards={("m2c-proxy", f"mw/{exe}/clip/trim.mp4"): "video:t"},
                   jobs={exe: {"outputPrefix": f"s3://m2c-proxy/mw/{exe}/clip/", "outputs": []}})
    assert find(store, OURS, f"projects/{PID}/sessions/s1/output/{exe}/trim.mp4") == "video:t"


def test_lineage_capture_follows_our_copy_to_the_original_too():
    store = _Store(guards={ORIGIN: ASSET})
    from unittest.mock import MagicMock
    cap = LineageCapture(table=MagicMock(), store=store, ingest_bucket=OURS,
                         s3=_S3({(OURS, COPY): {"origin": f"s3://{ORIGIN[0]}/{ORIGIN[1]}"}}))
    cap.edges = _Edges()
    assert cap.resolve(PID, f"s3://{OURS}/{COPY}") == (ASSET, "")
    assert cap.resolve(PID, f"s3://{OURS}/projects/x/unknown.wav") == (None, "untracked_location")
