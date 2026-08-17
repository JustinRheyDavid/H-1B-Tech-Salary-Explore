"""Move the Parquet cache between this machine and Azure Blob Storage.

Plan Step 7. Two directions, one credential:

* :func:`upload_raw` runs on a laptop, authenticated as you.
* :func:`download_raw` runs inside the ETL container, authenticated as the
  ``h1b-etl`` managed identity.

Both call :class:`~azure.identity.DefaultAzureCredential`, which tries a chain
of sources and takes the first that works — ``az login`` locally, the instance
metadata endpoint in Azure. That is the whole reason the same two functions
serve both callers with no branching and no configuration flag.

**There is no account key anywhere, and there cannot be.** ``storage.bicep``
sets ``allowSharedKeyAccess: false``, so connection strings and SAS tokens
derived from a key are not merely discouraged here — they do not work. Access is
Entra-only, which also means an ``AuthorizationPermissionMismatch`` is a missing
*role assignment*, never a missing password. See docs/azure-runbook.md §4:
``h1b-etl`` gets Storage Blob Data Contributor from ``infra/roles.bicep``, and a
human operator needs the same role granted by hand.

**The cache, not the spreadsheets.** ``data/raw/`` holds nine ``.xlsx`` totalling
851 MB; ``data/interim/`` holds the nine Parquet conversions of them at 175 MB,
about 4.9x smaller. Both fit the free 5 GB allowance, but only one is worth the
upload time, and the loader reads Parquet anyway — re-reading the spreadsheets
takes roughly 15 minutes against seconds for the cache.

**These blobs are on a 90-day timer.** ``storage.bicep`` attaches a lifecycle
rule deleting anything under ``raw/`` after ``rawRetentionDays`` (90). That is
deliberate — the cache is rebuildable from ``data/raw/`` by ``src.ingest`` — but
it means an ETL job run four months from now finds an empty container rather
than a permission error, and the fix is to upload again, not to debug access.
"""

from __future__ import annotations

import os
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient

__all__ = [
    "CURATED_CONTAINER",
    "RAW_CONTAINER",
    "account_name",
    "cache_files",
    "container_client",
    "download_raw",
    "list_raw",
    "upload_raw",
]

RAW_CONTAINER = "raw"
CURATED_CONTAINER = "curated"

# The storage account name is derived in Bicep from
# uniqueString(resourceGroup().id, location), so it is stable for this
# deployment but not guessable and not portable. The environment variable is
# what Step 9 will set on the container job; the fallback is what makes the
# functions usable from a laptop without ceremony.
_ACCOUNT_ENV = "H1B_STORAGE_ACCOUNT"
_DEFAULT_ACCOUNT = "sth1bhutymqa65yoty"

# Only DOL disclosure caches, not everything Parquet-shaped in the directory.
#
# Two separate hazards, one pattern. The glob exists because DOL published
# FY2026 as "LCA_Dislclosure_Data_FY2026_Q2" — misspelling "Disclosure" — so
# naming files explicitly guarantees dropping one; src.ingest.source_files
# globs for exactly this reason and this must not reintroduce the bug. The
# "LCA_" prefix exists because data/interim/ also accumulates build artefacts:
# probe.parquet sits there at 1.6 KB, and a bare *.parquet glob would upload it
# as a tenth blob, quietly breaking Step 7's own "nine blobs" check.
_CACHE_GLOB = "LCA_*.parquet"


def account_name() -> str:
    """Storage account to talk to, from ``H1B_STORAGE_ACCOUNT`` or the default."""
    return os.environ.get(_ACCOUNT_ENV) or _DEFAULT_ACCOUNT


def container_client(container: str = RAW_CONTAINER) -> ContainerClient:
    """A client for ``container``, authenticated as whoever is running this.

    Constructing this does no network I/O and does not prove the credential is
    valid — authentication happens on the first real request, so a bad identity
    surfaces at the upload, not here.
    """
    service = BlobServiceClient(
        account_url=f"https://{account_name()}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
        # Chunk everything above 8 MiB instead of the SDK's 64 MiB default.
        #
        # Not a tuning preference — the default breaks this upload. Every cache
        # file is 13-33 MiB, so all nine sit under the default threshold and go
        # as ONE PUT each. A single request carrying 28 MiB up a domestic uplink
        # outlives the socket write timeout, and the first attempt here died on
        # the third file with:
        #
        #   ServiceResponseError: ('Connection aborted.',
        #                          TimeoutError('The write operation timed out'))
        #
        # A single PUT also has nothing to retry: the SDK can resend a failed
        # 8 MiB block, but a failed whole-file PUT restarts the whole file.
        # Chunking is what makes max_concurrency mean anything too — below the
        # threshold there is only one part, so parallelism was a no-op.
        max_single_put_size=8 * 1024 * 1024,
        max_block_size=8 * 1024 * 1024,
        connection_timeout=60,
        read_timeout=300,
    )
    return service.get_container_client(container)


def cache_files(interim_dir: Path) -> list[Path]:
    """Every DOL Parquet cache in ``interim_dir``, in a stable order.

    Matched by glob rather than by name, and by prefix rather than by
    extension — see :data:`_CACHE_GLOB` for why both halves matter.
    """
    files = sorted(Path(interim_dir).glob(_CACHE_GLOB))
    if not files:
        raise FileNotFoundError(
            f"No {_CACHE_GLOB} files in {interim_dir}. "
            "Build the cache first: python -c 'from src import ingest; ingest.load_all()'"
        )
    return files


def upload_raw(
    local_path: Path | str,
    *,
    overwrite: bool = True,
    container: str = RAW_CONTAINER,
) -> str:
    """Upload one file to the ``raw`` container. Returns the blob name.

    ``container`` exists for two callers. Step 9 writes cleaned output to
    ``curated``, and the round-trip tests need somewhere to scribble that is not
    ``raw`` — the exact contents of ``raw`` are Step 7's acceptance criterion, so
    a test that crashes between upload and cleanup there would leave a tenth blob
    and invalidate the count it is meant to protect.

    The blob name is the file name — the container is already the namespace, so
    a prefix would only make ``download_raw`` callers reconstruct it.

    Streamed from the open file handle rather than read into memory: the largest
    cache file is 33 MB, which would be survivable, but the spreadsheets these
    come from are up to 143 MB and the same function should not become a
    landmine if someone points it at one.

    ``overwrite=True`` by default because this uploads a *cache* — the blobs are
    reproducible from ``data/raw/`` and carry no state worth protecting. Pass
    ``overwrite=False`` to make an existing blob raise instead.
    """
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {path}")

    client = container_client(container)
    with path.open("rb") as handle:
        client.upload_blob(
            name=path.name,
            data=handle,
            overwrite=overwrite,
            # Parallel block upload. The default of 1 makes a 33 MB file a
            # single serial stream for no reason.
            max_concurrency=4,
        )
    return path.name


def download_raw(
    blob_name: str, dest: Path | str, *, container: str = RAW_CONTAINER
) -> Path:
    """Download ``blob_name`` from ``raw`` to ``dest``. Returns the local path.

    If ``dest`` is a directory the blob keeps its own name inside it; otherwise
    ``dest`` is the full target path. Parent directories are created.

    Written to a scratch file and renamed into place, mirroring
    :func:`src.ingest._build`. A half-downloaded Parquet file that kept the real
    name would be indistinguishable from a good one on the next run, and pandas
    would fail somewhere much less obvious than here.
    """
    dest = Path(dest)
    target = dest / blob_name if dest.is_dir() else dest
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_suffix(f"{target.suffix}.{os.getpid()}.tmp")
    client = container_client(container)
    try:
        with tmp.open("wb") as handle:
            client.download_blob(blob_name, max_concurrency=4).readinto(handle)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def list_raw() -> list[tuple[str, int]]:
    """Every blob in ``raw`` as ``(name, size_in_bytes)``, sorted by name."""
    client = container_client()
    return sorted((b.name, b.size) for b in client.list_blobs())


def _mib(count: int) -> str:
    return f"{count / 1024 / 1024:8.1f} MiB"


def main(argv: list[str] | None = None) -> int:
    """``python -m src.etl.blob {upload,download,list}``.

    Exists so the Step 7 upload is a command someone can re-run and a reviewer
    can read, rather than a snippet pasted into a shell once and lost. Step 9's
    container will import the functions directly and never touch this.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="upload the Parquet cache to raw/")
    up.add_argument("--interim", type=Path, default=Path("data/interim"))
    up.add_argument(
        "--force",
        action="store_true",
        help="re-upload even when a blob of the same size is already there",
    )

    down = sub.add_parser("download", help="download one blob")
    down.add_argument("blob_name")
    down.add_argument("dest", type=Path)

    sub.add_parser("list", help="list what is in raw/")

    args = parser.parse_args(argv)

    if args.command == "list":
        blobs = list_raw()
        for name, size in blobs:
            print(f"{_mib(size)}  {name}")
        print(f"{len(blobs)} blobs, {_mib(sum(s for _, s in blobs))} total")
        return 0

    if args.command == "download":
        print(download_raw(args.blob_name, args.dest))
        return 0

    # Skipping same-size blobs keeps a re-run cheap. Size is a weak identity
    # check, but these are immutable published quarterlies, not files being
    # edited — and --force is there for when that assumption breaks.
    existing = dict(list_raw())
    for path in cache_files(args.interim):
        local = path.stat().st_size
        if not args.force and existing.get(path.name) == local:
            print(f"{_mib(local)}  {path.name}  (already uploaded, skipping)")
            continue
        upload_raw(path)
        print(f"{_mib(local)}  {path.name}  uploaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
