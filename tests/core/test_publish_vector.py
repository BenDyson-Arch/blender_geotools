"""Vector publish (geotools.publish.vector and the vector branch of BuildJob).

Tests marked ``rusty`` run the real rusty-tiles wheel; the others substitute a
fake module. All features are synthetic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from geotools.core import crs_presets
from geotools.core.crs_match import DataCrs
from geotools.core.placement import Placement, PlacementMode
from geotools.core.vector import gpkg
from geotools.core.vector.model import Column, ColumnType, GeometryBuilder, Kind, VectorData
from geotools.publish import job as job_mod
from geotools.publish import staging as st
from geotools.publish import vector as pv
from geotools.publish.job import BuildJob, BuildRequest, JobState

try:
    import rusty_tiles
except ImportError:  # pragma: no cover - depends on the environment
    rusty_tiles = None

rusty = pytest.mark.skipif(rusty_tiles is None, reason="needs the rusty-tiles wheel")

# An invented site in GDA2020 / MGA zone 56 (synthetic numbers).
E0, N0, A0 = 502_000.0, 6_961_000.0, 10.0
MGA56 = crs_presets.get("MGA2020_56")
assert MGA56 is not None


def contexts(n: int = 6, *, shift: float = 0.0, depth: float = 0.0) -> VectorData:
    """``n`` square polygons with varying Z (draped), a float and a text column."""
    builder = GeometryBuilder()
    for i in range(n):
        x, y = E0 + 10.0 * i + shift, N0
        z = A0 + i
        ring = np.array(
            [[x, y, z], [x + 5, y, z + 0.5], [x + 5, y + 5, z + 1], [x, y + 5, z + 0.2], [x, y, z]]
        )
        builder.add_polygon(i, [ring])
    rows = np.arange(n)
    return VectorData(
        name="contexts",
        fids=rows + 1,
        columns={
            "depth_top": Column("depth_top", ColumnType.FLOAT, rows * 0.1 + depth),
            "context": Column("context", ColumnType.STRING, [f"C{100 + i}" for i in rows]),
        },
        geometries=builder.finish(),
        crs=None,
    )


def project() -> DataCrs:
    return DataCrs(MGA56.epsg, MGA56.definition, "project")


def source(data: VectorData | None = None, *, reuse: bool = True, **options) -> pv.VectorSource:
    data = data if data is not None else contexts()
    placement = pv.placement_kwargs(
        Placement(
            mode=PlacementMode.SOURCE_CRS,
            source_crs=MGA56.definition,
            source_offset=(E0, N0, A0),
            height_offset=0.0,
        )
    )
    return pv.VectorSource(
        layers=(pv.VectorLayer("contexts", data, Kind.POLYGONS, "contexts"),),
        crs=project(),
        options={**placement, "reproducible": True, **options},
        reuse=reuse,
    )


def request(output: Path, src: pv.VectorSource, **kwargs: object) -> BuildRequest:
    base: dict[str, object] = {
        "output": output,
        "placement_kwargs": {k: src.options[k] for k in ("source_crs", "height_offset")},
        "force": True,
        "extra_record": {"geotools_version": "test", "build_profile": "Vectors"},
        "vector": src,
    }
    base.update(kwargs)
    return BuildRequest(**base)  # type: ignore[arg-type]


def run(job: BuildJob, timeout: float = 120.0):
    job.start()
    assert job.join(timeout), "job did not finish"
    return job.poll_events()


# --- placement --------------------------------------------------------------------------


def test_placement_kwargs_pass_the_project_crs_as_written() -> None:
    preset = Placement(
        mode=PlacementMode.SOURCE_CRS,
        source_crs=MGA56.definition,
        source_offset=(E0, N0, A0),
        height_offset=-1.25,
    )
    # The preset's PROJ definition, which the bundled 0.4.0 accepts; never the
    # EPSG code the GeoPackage declares.
    assert pv.placement_kwargs(preset) == {"source_crs": MGA56.definition, "height_offset": -1.25}
    # A plain EPSG code passes through unchanged (rusty-tiles 0.4.1's allow-list).
    epsg = Placement(mode=PlacementMode.SOURCE_CRS, source_crs=" EPSG:7856 ")
    assert pv.placement_kwargs(epsg) == {"source_crs": "EPSG:7856", "height_offset": 0.0}
    assert pv.placement_kwargs(Placement(mode=PlacementMode.LOCAL)) == {"source_crs": "local"}


def test_placement_kwargs_refuse_anchor_and_invalid() -> None:
    with pytest.raises(ValueError, match="cannot anchor"):
        pv.placement_kwargs(Placement(mode=PlacementMode.ANCHOR, anchor=(15.0, 45.0, 0.0)))
    with pytest.raises(ValueError):
        pv.placement_kwargs(Placement(mode=PlacementMode.SOURCE_CRS, source_crs="7856"))


def test_table_names_are_stable_and_distinct() -> None:
    names = pv.table_names(
        [
            ("contexts", Kind.POLYGONS),
            ("contexts", Kind.LINES),
            ("Site.contexts", Kind.POLYGONS),
            ("Site contexts", Kind.POLYGONS),
            ("SITE_CONTEXTS", Kind.POLYGONS),
            ("3d", Kind.POINTS),
            ("...", Kind.POLYGONS),
            ("dig.lines", Kind.LINES),
            ("dig.points", Kind.POINTS),
        ]
    )
    assert names == [
        "contexts",
        "contexts_lines",
        "Site_contexts",
        "Site_contexts_2",
        "SITE_CONTEXTS_3",
        "layer_3d_points",
        "layer",
        "dig_lines",
        "dig_points",
    ]
    assert pv.table_names([("x" * 100, Kind.LINES)]) == ["x" * 54 + "_lines"]


def test_is_reuse_refusal() -> None:
    for message in (
        (
            "previous encoder, CRS or conversion settings differ; run a fresh conversion "
            "without reuseTileset"
        ),
        "invalid previous payload record: abc",
        "previous archive has legacy b3dm polygon fills",
    ):
        assert pv.is_reuse_refusal(RuntimeError(message))
    assert not pv.is_reuse_refusal(RuntimeError("6 unconvertible feature(s)"))


def test_write_source_keeps_fids_and_binary64(tmp_path: Path) -> None:
    src = source()
    path = tmp_path / "v.gpkg"
    path.write_bytes(b"stale")  # replaced, not appended to
    records = pv.write_source(src, path)
    assert records == [
        {"table": "contexts", "object": "contexts", "kind": "polygons", "features": 6}
    ]
    data = gpkg.read_layer(path, "contexts")
    assert data.fids.tolist() == [1, 2, 3, 4, 5, 6]
    coords = data.geometries[Kind.POLYGONS].coords
    assert coords[0].tolist() == [E0, N0, A0]  # binary64, not rounded through float32
    with sqlite3.connect(path) as con:
        srs = con.execute(
            "SELECT organization, organization_coordsys_id FROM gpkg_spatial_ref_sys "
            "WHERE srs_id = 7856"
        ).fetchone()
    assert srs == ("EPSG", 7856)
    with pytest.raises(ValueError):
        pv.write_source(pv.VectorSource(layers=(), crs=None), path)


def test_summarise_vector_report() -> None:
    report = {
        "features": 6,
        "tiles": 3,
        "leafTiles": 2,
        "skippedFeatures": 0,
        "layers": [{"name": "contexts", "features": 6, "sourceCrs": "x", "jsonFields": []}],
        "reuse": {"previousTileset": True, "reusedContents": 2, "rebuiltContents": 1, "x": 1},
        "lodFallbacks": [{"sourceId": "1"}],
    }
    summary = pv.summarise_vector_report(report)
    assert summary["features"] == 6 and summary["tiles"] == 3
    assert summary["layers"] == [{"name": "contexts", "features": 6, "sourceCrs": "x"}]
    assert summary["reuse"] == {"previousTileset": True, "reusedContents": 2, "rebuiltContents": 1}
    assert "lodFallbacks" not in summary


# --- the job with a fake rusty-tiles ---------------------------------------------------


def fake_rt(calls: list[dict], refuse_reuse: bool = False) -> SimpleNamespace:
    def vector_to_3tz(input, output, *, callback=None, force=False, reuse_tileset=None, **kw):
        assert Path(input).is_file()
        assert reuse_tileset is None or Path(reuse_tileset) != Path(output)
        calls.append({"input": Path(input), "reuse_tileset": reuse_tileset, "force": force, **kw})
        if reuse_tileset is not None:
            assert Path(reuse_tileset).read_bytes() == Path(output).read_bytes()
            if refuse_reuse:
                raise RuntimeError(
                    "previous encoder differs; run a fresh portable conversion without reuseTileset"
                )
        if callback:
            callback({"event": "progress", "phase": "encode", "done": 1, "total": 2})
        Path(output).write_bytes(b"vector archive %d" % len(calls))
        reuse = {"previousTileset": reuse_tileset is not None, "reusedContents": 1}
        return SimpleNamespace(
            report={"features": 6, "tiles": 2, "reuse": reuse}, cleanup_diagnostics=()
        )

    def validate(path, **_kwargs):
        return {"ok": True, "tiles": 2, "checks": ["a"]}

    return SimpleNamespace(vector_to_3tz=vector_to_3tz, validate=validate)


def test_vector_job_writes_tiles_and_records(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: fake_rt(calls))
    out = tmp_path / "contexts.3tz"
    src = source()
    job = BuildJob(None, request(out, src))
    events = run(job)
    assert job.state is JobState.DONE, job.error
    assert [e.state for e in events if e.message][:2] == [JobState.WRITING, JobState.TILING]
    (call,) = calls
    assert call["source_crs"] == MGA56.definition and call["height_offset"] == 0.0
    assert call["layers"] == ["contexts"] and call["reuse_tileset"] is None
    assert call["reproducible"] is True and call["force"] is True

    staging = st.staging_dir_for(out)
    assert (staging / st.VECTORS_NAME).is_file()
    assert not (staging / st.WORK_DIR).exists()
    record = json.loads((staging / st.RECORD_NAME).read_text())
    assert record["output_type"] == "vector"
    assert record["placement"]["source_crs"] == MGA56.definition
    assert record["placement"]["crs_epsg"] == "EPSG:7856"
    assert record["vectors"]["tables"][0]["features"] == 6
    assert record["vectors"]["crs"] == "EPSG:7856"
    reuse = record["vectors"]["reuse"]
    assert (reuse["requested"], reuse["used"], reuse["reason"]) == (
        True,
        False,
        "no previous archive",
    )
    assert reuse["report"]["previousTileset"] is False
    assert record["settings"]["reproducible"] is True
    assert "source_crs" not in record["settings"]
    assert record["validation"]["ok"] is True
    assert job.result is not None and job.result.report_summary["features"] == 6

    # Republish: the previous archive goes in as a copy, never the output itself.
    calls.clear()
    job = BuildJob(None, request(out, source(contexts(depth=1.0))))
    events = run(job)
    assert job.state is JobState.DONE, job.error
    (call,) = calls
    assert call["reuse_tileset"] is not None
    record = json.loads((staging / st.RECORD_NAME).read_text())
    assert record["vectors"]["reuse"]["used"] is True
    assert "(incremental)" in (events[-1].message or "")
    assert not (staging / st.WORK_DIR / st.PREVIOUS_NAME).exists()


def test_vector_job_rebuilds_when_reuse_is_refused(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []
    out = tmp_path / "contexts.3tz"
    out.write_bytes(b"old archive")
    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: fake_rt(calls, refuse_reuse=True))
    job = BuildJob(None, request(out, source()))
    events = run(job)
    assert job.state is JobState.DONE, job.error
    assert [c["reuse_tileset"] is not None for c in calls] == [True, False]
    assert any("cannot be reused" in (e.message or "") for e in events)
    record = json.loads((st.staging_dir_for(out) / st.RECORD_NAME).read_text())
    assert record["vectors"]["reuse"]["used"] is False
    assert "previous encoder differs" in record["vectors"]["reuse"]["refused"]


def test_vector_job_without_reuse_and_other_failures(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []
    out = tmp_path / "contexts.3tz"
    out.write_bytes(b"old archive")
    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: fake_rt(calls))
    job = BuildJob(None, request(out, source(reuse=False)))
    run(job)
    assert job.state is JobState.DONE and calls[0]["reuse_tileset"] is None

    def broken(*_args, **_kwargs):
        raise RuntimeError("6 unconvertible feature(s)")

    monkeypatch.setattr(
        job_mod, "_rusty_tiles", lambda: SimpleNamespace(vector_to_3tz=broken, validate=None)
    )
    job = BuildJob(None, request(out, source()))
    run(job)
    assert job.state is JobState.FAILED
    assert "unconvertible" in (job.error or "")
    assert out.read_bytes() == b"vector archive 1"  # the previous archive is kept
    work = st.staging_dir_for(out) / st.WORK_DIR
    assert (work / st.VECTORS_NAME).is_file()  # kept for debugging, with error.txt
    assert not (work / st.PREVIOUS_NAME).exists()  # but not the copy of the archive


def test_vector_request_needs_a_3tz(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BuildJob(None, request(tmp_path / "x.glb", source()))
    with pytest.raises(ValueError):
        BuildJob(None, BuildRequest(output=tmp_path / "x.3tz", placement_kwargs={}))


# --- the real rusty-tiles ---------------------------------------------------------------


@rusty
def test_vector_job_real_rusty_tiles_with_reuse(tmp_path: Path) -> None:
    """A preset CRS (PROJ with +towgs84) is passed explicitly, the archive validates,
    and an attribute edit republishes incrementally."""
    out = tmp_path / "contexts.3tz"
    job = BuildJob(None, request(out, source()))
    run(job)
    assert job.state is JobState.DONE, job.error
    assert job.result is not None and job.result.validation_ok is True
    staging = st.staging_dir_for(out)
    record = json.loads((staging / st.RECORD_NAME).read_text())
    assert record["report_summary"]["features"] == 6
    (layer,) = record["report_summary"]["layers"]
    assert layer["sourceCrs"] == MGA56.definition and layer["heightOffset"] == 0.0
    assert record["vectors"]["reuse"]["used"] is False

    # Republish unchanged: everything is reused.
    job = BuildJob(None, request(out, source()))
    run(job)
    assert job.state is JobState.DONE, job.error
    record = json.loads((staging / st.RECORD_NAME).read_text())
    reuse = record["vectors"]["reuse"]
    assert reuse["used"] is True and reuse["report"]["previousTileset"] is True
    assert reuse["report"]["rebuiltContents"] == 0
    assert record["validation"]["ok"] is True

    # Changed settings (other LOD levels): rusty-tiles refuses the baseline, the job
    # says so and builds afresh.
    job = BuildJob(None, request(out, source(lod_levels=2)))
    run(job)
    assert job.state is JobState.DONE, job.error
    record = json.loads((staging / st.RECORD_NAME).read_text())
    assert record["vectors"]["reuse"]["used"] is False
    assert "previous" in record["vectors"]["reuse"]["refused"]
    assert rusty_tiles.validate(out)["ok"]
