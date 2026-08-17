"""Tests for :mod:`src.etl.blob`.

Split deliberately in two.

The file-selection tests are pure and always run: they are where the two real
bugs live. A bare ``*.parquet`` glob silently uploads ``probe.parquet`` as a
tenth blob, and naming the source files explicitly silently drops FY2026, whose
published filename misspells "Disclosure". Neither failure raises anything — the
first quietly breaks Step 7's own acceptance count, the second quietly loses a
quarter of the data — so they are checked without needing an Azure account.

The round-trip tests need a real storage account and are marked ``azure``. They
are skipped when the SDK is missing, when nobody is logged in, or when the
container is unreachable, so a fresh clone still gets a green suite. They write
to ``curated``, never to ``raw`` — the exact contents of ``raw`` are Step 7's
acceptance criterion, and a test that scribbles there could invalidate the very
count it is meant to protect.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("azure.storage.blob", reason="Azure SDK not installed")

from azure.core.exceptions import ResourceNotFoundError  # noqa: E402

from src.etl import blob  # noqa: E402  (after importorskip, by design)

CURATED = blob.CURATED_CONTAINER


# --------------------------------------------------------------------------
# File selection — no network, always runs.
# --------------------------------------------------------------------------


def _make_cache(tmp_path: Path, names: list[str]) -> Path:
    for name in names:
        (tmp_path / name).write_bytes(b"")
    return tmp_path


def test_cache_files_ignores_artefacts_that_are_not_dol_data(tmp_path):
    """``probe.parquet`` really is in ``data/interim/``, at 1.6 KB.

    A ``*.parquet`` glob would upload it, making ten blobs where Step 7's
    acceptance criterion expects nine — and nothing would raise.
    """
    directory = _make_cache(
        tmp_path,
        [
            "LCA_Disclosure_Data_FY2024_Q1.parquet",
            "LCA_Disclosure_Data_FY2024_Q2.parquet",
            "probe.parquet",
            "scratch.parquet",
        ],
    )
    selected = [p.name for p in blob.cache_files(directory)]
    assert selected == [
        "LCA_Disclosure_Data_FY2024_Q1.parquet",
        "LCA_Disclosure_Data_FY2024_Q2.parquet",
    ]


def test_cache_files_keeps_the_file_dol_misspelled(tmp_path):
    """FY2026 is published as ``LCA_Dislclosure_Data_FY2026_Q2``.

    That is DOL's typo, not one here. Any selection matching on the correct
    spelling drops a whole quarter of filings and still succeeds, so this asserts
    the misspelled name survives selection rather than asserting what the glob
    pattern happens to be.
    """
    directory = _make_cache(
        tmp_path,
        [
            "LCA_Disclosure_Data_FY2025_Q4.parquet",
            "LCA_Dislclosure_Data_FY2026_Q2.parquet",
        ],
    )
    assert [p.name for p in blob.cache_files(directory)] == [
        "LCA_Disclosure_Data_FY2025_Q4.parquet",
        "LCA_Dislclosure_Data_FY2026_Q2.parquet",
    ]


def test_cache_files_order_is_stable(tmp_path):
    """Callers upload in this order and the runbook records counts by name."""
    directory = _make_cache(
        tmp_path,
        [
            "LCA_Disclosure_Data_FY2025_Q1.parquet",
            "LCA_Disclosure_Data_FY2024_Q1.parquet",
            "LCA_Disclosure_Data_FY2024_Q3.parquet",
        ],
    )
    assert blob.cache_files(directory) == sorted(blob.cache_files(directory))


def test_cache_files_says_how_to_build_the_cache_when_it_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="ingest"):
        blob.cache_files(tmp_path)


def test_account_name_prefers_the_environment(monkeypatch):
    monkeypatch.setenv(blob._ACCOUNT_ENV, "stsomewhereelse")
    assert blob.account_name() == "stsomewhereelse"


def test_account_name_falls_back_to_the_deployed_account(monkeypatch):
    monkeypatch.delenv(blob._ACCOUNT_ENV, raising=False)
    assert blob.account_name() == blob._DEFAULT_ACCOUNT


def test_upload_rejects_a_directory(tmp_path):
    """Caught locally, before any credential is constructed."""
    with pytest.raises(FileNotFoundError):
        blob.upload_raw(tmp_path)


def test_scratch_paths_are_unique_per_call(tmp_path):
    """Two concurrent downloads must not agree on one scratch file.

    The process id alone is not enough — threads in one process share it — and
    ``src.ingest._build`` already carries this scar. If they collide, one
    thread's rename pulls the file from under the other and the loser's cleanup
    deletes what the winner is still writing, producing a truncated Parquet that
    fails much later inside pandas.

    Nothing threads today. Step 9 downloads nine blobs, where a thread pool is
    the obvious speedup, so this is a guard on a future change rather than on
    current behaviour.
    """
    target = tmp_path / "LCA_Disclosure_Data_FY2024_Q1.parquet"
    paths = {blob._scratch_path(target) for _ in range(100)}
    assert len(paths) == 100


def test_scratch_path_does_not_collide_with_the_real_file(tmp_path):
    """It must also not be mistaken for data, or cleaned up as if it were."""
    target = tmp_path / "LCA_Disclosure_Data_FY2024_Q1.parquet"
    scratch = blob._scratch_path(target)
    assert scratch != target
    assert scratch.name.startswith(target.name)
    assert scratch.suffix == ".tmp"


# --------------------------------------------------------------------------
# Round trip — needs a real account.
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def scratch_container():
    """The live ``curated`` container, or skip.

    Deliberately NOT ``raw``. The exact contents of ``raw`` are Step 7's
    acceptance criterion — nine blobs, nothing else — and a test that adds and
    removes blobs there is one crashed process away from invalidating the count
    it exists to protect. ``curated`` is empty until Step 9 and costs nothing to
    scribble in.

    Skips rather than fails on any error: no login, no role assignment, no
    network. This suite must stay green on a machine that has never seen Azure.
    """
    try:
        client = blob.container_client(blob.CURATED_CONTAINER)
        client.get_container_properties()
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot test"
        pytest.skip(f"{blob.account_name()}/{blob.CURATED_CONTAINER} unreachable: {exc}")
    return client


@pytest.fixture
def temp_blob(scratch_container):
    """A uniquely named blob, deleted afterwards however the test ends.

    The name is deliberately outside ``LCA_*`` so a leaked blob cannot be
    mistaken for data and ``cache_files`` would never pick it up.

    Teardown tolerates the blob not existing. A test that fails *before*
    uploading is exactly when you are debugging, and an unconditional delete
    raises ``ResourceNotFoundError`` on top of the real failure and buries it.
    """
    name = f"pytest-roundtrip-{os.getpid()}.bin"
    yield name
    try:
        scratch_container.delete_blob(name, delete_snapshots="include")
    except ResourceNotFoundError:
        pass


@pytest.mark.azure
def test_upload_then_download_returns_identical_bytes(tmp_path, temp_blob):
    payload = os.urandom(64 * 1024)
    source = tmp_path / temp_blob
    source.write_bytes(payload)

    assert blob.upload_raw(source, container=CURATED) == temp_blob

    restored = blob.download_raw(
        temp_blob, tmp_path / "restored.bin", container=CURATED
    )
    assert restored.read_bytes() == payload


@pytest.mark.azure
def test_download_into_a_directory_keeps_the_blob_name(tmp_path, temp_blob):
    source = tmp_path / temp_blob
    source.write_bytes(b"x" * 1024)
    blob.upload_raw(source, container=CURATED)

    destination = tmp_path / "out"
    destination.mkdir()
    got = blob.download_raw(temp_blob, destination, container=CURATED)
    assert got.name == temp_blob


@pytest.mark.azure
def test_download_leaves_no_scratch_file_behind(tmp_path, temp_blob):
    source = tmp_path / temp_blob
    source.write_bytes(b"y" * 1024)
    blob.upload_raw(source, container=CURATED)

    destination = tmp_path / "out"
    destination.mkdir()
    blob.download_raw(temp_blob, destination, container=CURATED)
    assert not list(destination.glob("*.tmp"))


@pytest.mark.azure
def test_raw_holds_exactly_the_nine_dol_caches():
    """Step 7's acceptance criterion, as a test rather than a one-off check.

    Catches a stray blob left in ``raw`` by anything — a crashed upload, a test
    pointed at the wrong container, a manual experiment.
    """
    names = [name for name, _ in blob.list_raw()]

    # This assertion has a built-in expiry that is not a code defect.
    # storage.bicep deletes everything under raw/ after rawRetentionDays (90),
    # so an empty container roughly 90 days after the last upload is the
    # retention policy working, not a broken upload. Say so here — otherwise the
    # failure reads as "someone broke Step 7" and the diagnosis lives only in the
    # runbook.
    assert names, (
        "raw/ is empty. If it has been ~90 days since the last upload this is "
        "storage.bicep's lifecycle rule doing its job, not a defect — re-run "
        "'python -m src.etl.blob upload'. If it has not, something deleted them."
    )
    assert len(names) == 9, f"expected 9 DOL caches, found {len(names)}: {names}"
    assert all(
        n.startswith("LCA_") and n.endswith(".parquet") for n in names
    ), f"unexpected blob in raw/, which should hold only DOL caches: {names}"
    assert "LCA_Dislclosure_Data_FY2026_Q2.parquet" in names, (
        "the misspelled FY2026 cache is missing; a selection matching the "
        f"correct spelling drops a whole quarter. Found: {names}"
    )
