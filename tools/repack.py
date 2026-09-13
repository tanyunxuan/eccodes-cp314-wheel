#!/usr/bin/env python3
"""Repackage official ECMWF ``eccodes`` cp313 binary wheels for cp314.

The ecCodes native libraries shipped by the official binary wheels are C
ABI artifacts and are independent of the CPython version.  The only thing
that ties those wheels to CPython 3.13 is:

1. the wheel compatibility tag (``cp313-cp313-<platform>``) stored in
   ``*.dist-info/WHEEL`` and in the wheel filename;
2. a tiny compiled "glue" extension ``eccodes/_eccodes`` (a
   version-independent :mod:`ctypes` reimplementation of which lives next
   to this script, see ``_eccodes_shim.py``).

This tool downloads nothing itself and compiles nothing.  It takes an
official cp313 wheel, drops the compiled glue extension, inserts the pure
Python shim, rewrites the tag to ``cp314-cp314-<platform>``, regenerates
``RECORD`` and writes a correctly named cp314 wheel.  Every other file
(including all upstream Python sources) is copied byte-for-byte unchanged.

Subcommands
-----------

``discover``
    Query the PyPI JSON API and emit the build matrix (newest upstream
    version that provides a cp313 wheel, per target platform) as JSON.

``repack``
    Convert one downloaded cp313 wheel into a cp314 wheel.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.request import urlopen

SOURCE_ABI_TAG: str = "cp313-cp313"
TARGET_ABI_TAG: str = "cp314-cp314"
PYPI_JSON_URL: str = "https://pypi.org/pypi/eccodes/json"
SHIM_WHEEL_PATH: str = "eccodes/_eccodes.py"
DEFAULT_SHIM_PATH: Path = Path(__file__).with_name("_eccodes_shim.py")

# Platform target -> (wheel filename suffix template, CI runner label).
TARGETS: Tuple[Tuple[str, str, str], ...] = (
    ("win_amd64", "eccodes-{version}-cp313-cp313-win_amd64.whl", "windows-latest"),
    (
        "manylinux_2_28_x86_64",
        "eccodes-{version}-cp313-cp313-manylinux_2_28_x86_64.whl",
        "ubuntu-latest",
    ),
)

_STABLE_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_COMPILED_GLUE_RE = re.compile(r"^eccodes/_eccodes\..*\.(?:pyd|so)$")


@dataclass(frozen=True)
class Artifact:
    """A single wheel that needs repackaging.

    Attributes:
        version: Upstream ``eccodes`` release version, e.g. ``"2.48.0"``.
        platform: Platform token as used in wheel tags, e.g.
            ``"win_amd64"`` or ``"manylinux_2_28_x86_64"``.
        filename: Filename of the source cp313 wheel on PyPI.
        url: Direct download URL of the source cp313 wheel.
        runner: GitHub Actions runner label that must verify this wheel.
        kind: Matrix entry kind (always ``"repack"``).
    """

    version: str
    platform: str
    filename: str
    url: str
    runner: str
    kind: str = "repack"

    @property
    def output_filename(self) -> str:
        """Filename of the repackaged cp314 wheel."""
        return self.filename.replace(SOURCE_ABI_TAG, TARGET_ABI_TAG)


def _version_key(version: str) -> Tuple[int, ...]:
    """Convert a dotted numeric version into a sortable tuple.

    Args:
        version: A stable version string such as ``"2.48.0"``.

    Returns:
        A tuple of integers suitable for ordering.
    """
    return tuple(int(part) for part in version.split("."))


def _is_stable(version: str) -> bool:
    """Return whether a version string is a plain stable release."""
    return bool(_STABLE_VERSION_RE.match(version))


def query_pypi() -> Dict[str, object]:
    """Fetch the ``eccodes`` project metadata from the PyPI JSON API.

    Returns:
        The parsed PyPI JSON document.
    """
    with urlopen(PYPI_JSON_URL, timeout=60) as response:  # noqa: S310 (fixed https URL)
        return json.load(response)


def discover(pypi_data: Dict[str, object]) -> List[Artifact]:
    """Find the newest repackagable cp313 wheel for each target platform.

    For Windows this follows the newest ``eccodes`` release; for Linux the
    newest release that still ships a ``manylinux_2_28`` cp313 wheel
    (upstream moved the Linux native libraries to the separate
    ``eccodeslib`` package from 2.43.0 onwards).

    Args:
        pypi_data: Parsed PyPI JSON document for ``eccodes``.

    Returns:
        One :class:`Artifact` per available target platform, ordered as
        :data:`TARGETS`.
    """
    releases = pypi_data["releases"]  # type: ignore[index]
    versions = sorted(
        (v for v in releases.keys() if _is_stable(v)),  # type: ignore[union-attr]
        key=_version_key,
        reverse=True,
    )
    artifacts: List[Artifact] = []
    for platform, template, runner in TARGETS:
        wanted: Optional[Artifact] = None
        for version in versions:
            filename = template.format(version=version)
            match = next(
                (
                    file_meta
                    for file_meta in releases[version]  # type: ignore[index]
                    if file_meta["filename"] == filename
                ),
                None,
            )
            if match is not None:
                wanted = Artifact(
                    version=version,
                    platform=platform,
                    filename=filename,
                    url=match["url"],
                    runner=runner,
                )
                break
        if wanted is None:
            raise RuntimeError(
                f"no cp313 wheel found for platform target {platform!r}"
            )
        artifacts.append(wanted)
    return artifacts


def _record_hash(data: bytes) -> str:
    """Compute a RECORD-style ``sha256=...`` hash.

    Args:
        data: Raw file content.

    Returns:
        URL-safe base64, unpadded, prefixed with ``sha256=``.
    """
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_record(files: Dict[str, bytes], record_path: str) -> bytes:
    """Render a wheel ``RECORD`` file.

    Args:
        files: Mapping ``wheel path -> content`` of every shipped file.
        record_path: Wheel path of the RECORD file itself (stored with an
            empty hash, per PEP 427).

    Returns:
        The CSV-formatted RECORD content.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for path in sorted(files):
        if path == record_path:
            continue
        writer.writerow([path, _record_hash(files[path]), str(len(files[path]))])
    writer.writerow([record_path, "", ""])
    return buffer.getvalue().encode("utf-8")


def _require_single_wheel_tag(wheel_metadata: bytes, dist_info: str) -> str:
    """Extract and validate the unique compatibility tag from WHEEL data.

    Args:
        wheel_metadata: Raw content of ``*.dist-info/WHEEL``.
        dist_info: The ``.dist-info`` directory name (used in messages).

    Returns:
        The tag value, e.g. ``"cp313-cp313-win_amd64"``.

    Raises:
        ValueError: If zero or multiple tags are present, or the tag does
            not carry the expected cp313 ABI.
    """
    tags = [
        line.split(":", 1)[1].strip()
        for line in wheel_metadata.decode("utf-8").splitlines()
        if line.startswith("Tag:")
    ]
    if len(tags) != 1:
        raise ValueError(
            f"{dist_info}/WHEEL must contain exactly one Tag, found: {tags!r}"
        )
    tag = tags[0]
    if not tag.startswith(SOURCE_ABI_TAG + "-"):
        raise ValueError(f"unexpected source tag {tag!r}; expected cp313-cp313-*")
    return tag


def repack_wheel(
    source_wheel: Path,
    output_dir: Path,
    shim_path: Path = DEFAULT_SHIM_PATH,
) -> Path:
    """Convert an official cp313 binary wheel into a cp314 wheel.

    Args:
        source_wheel: Path to the downloaded ``*cp313*.whl`` file.
        output_dir: Directory where the new wheel is written.
        shim_path: Path to the pure-Python ``_eccodes`` shim source.

    Returns:
        Path of the newly written wheel.

    Raises:
        ValueError: On any structural or tag inconsistency, or when the
            expected bundled native libraries are not present.
    """
    with zipfile.ZipFile(source_wheel) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}

    dist_infos = {
        name.split("/", 1)[0]
        for name in entries
        if name.endswith(".dist-info/WHEEL")
    }
    if len(dist_infos) != 1:
        raise ValueError(f"expected exactly one .dist-info dir, got {dist_infos!r}")
    dist_info = next(iter(dist_infos))
    wheel_meta_path = f"{dist_info}/WHEEL"
    record_path = f"{dist_info}/RECORD"
    metadata_path = f"{dist_info}/METADATA"

    source_tag = _require_single_wheel_tag(entries[wheel_meta_path], dist_info)
    platform_token = source_tag[len(SOURCE_ABI_TAG) + 1 :]
    target_tag = f"{TARGET_ABI_TAG}-{platform_token}"

    # 1. drop the cp313 compiled glue extension (keep _eccodes.cc, if any)
    removed = sorted(name for name in entries if _COMPILED_GLUE_RE.match(name))
    if not removed:
        raise ValueError("no compiled eccodes/_eccodes extension found in wheel")
    for name in removed:
        del entries[name]

    # 2. insert the pure-Python shim
    if SHIM_WHEEL_PATH in entries:
        raise ValueError(f"{SHIM_WHEEL_PATH} already present; refusing to overwrite")
    entries[SHIM_WHEEL_PATH] = shim_path.read_bytes()

    # 3. rewrite compatibility tag
    wheel_text = entries[wheel_meta_path].decode("utf-8")
    entries[wheel_meta_path] = re.sub(
        r"^Tag: .*$", f"Tag: {target_tag}", wheel_text, flags=re.MULTILINE
    ).encode("utf-8")

    # 4. sanity checks on bundled native libraries
    if platform_token == "win_amd64":
        if "eccodes/eccodes.dll" not in entries:
            raise ValueError("eccodes/eccodes.dll missing from Windows wheel")
    elif platform_token.startswith("manylinux"):
        libeccodes = [
            name
            for name in entries
            if name.startswith("eccodes.libs/libeccodes-")
            and name.endswith(".so")
            and "memfs" not in name
        ]
        if not libeccodes:
            raise ValueError("no libeccodes shared object found in eccodes.libs")
    else:
        raise ValueError(f"unsupported platform token {platform_token!r}")
    if not entries[metadata_path].lstrip().startswith(b"Metadata-Version:"):
        raise ValueError("METADATA looks corrupted")

    # 5. regenerate RECORD (its own entry last, with empty hash)
    entries[record_path] = _build_record(entries, record_path)

    # 6. canonical output filename derived from wheel metadata (the source
    #    file itself may be named arbitrarily, e.g. "upstream.whl" in CI)
    dist_stem = dist_info[: -len(".dist-info")]
    distribution, _, dist_version = dist_stem.rpartition("-")
    escaped_name = re.sub(r"[^\w.]+", "_", distribution)
    output_name = f"{escaped_name}-{dist_version}-{target_tag}.whl"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_name

    # 7. write archive (sorted, deterministic timestamps, deflated)
    fixed_date = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for name in sorted(entries):
            info = zipfile.ZipInfo(filename=name, date_time=fixed_date)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            out.writestr(info, entries[name])

    print(f"source : {source_wheel.name}")
    print(f"  tag  : {source_tag}")
    print(f"  drop : {', '.join(removed)}")
    print(f"  add  : {SHIM_WHEEL_PATH} ({shim_path.name})")
    print(f"output : {output_name}")
    print(f"  tag  : {target_tag}")
    print(f"  path : {output_path}")
    return output_path


def _write_matrix(artifacts: Sequence[Artifact], matrix_file: Optional[Path]) -> str:
    """Serialise artifacts to a GitHub Actions ``include`` matrix document.

    Args:
        artifacts: The artifacts to build.
        matrix_file: Optional path to also write the document to.

    Returns:
        The JSON text.
    """
    matrix = {"include": [asdict(a) for a in artifacts]}
    text = json.dumps(matrix, indent=2)
    if matrix_file is not None:
        matrix_file.parent.mkdir(parents=True, exist_ok=True)
        matrix_file.write_text(text + "\n", encoding="utf-8")
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Command line arguments without program name (``None`` means
            :data:`sys.argv`).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    discover_parser = sub.add_parser(
        "discover", help="discover newest repackagable cp313 wheels on PyPI"
    )
    discover_parser.add_argument(
        "--matrix-file",
        type=Path,
        default=None,
        help="write the GitHub Actions matrix JSON to this path",
    )

    repack_parser = sub.add_parser("repack", help="repack one cp313 wheel as cp314")
    repack_parser.add_argument("--wheel", type=Path, required=True)
    repack_parser.add_argument("--out-dir", type=Path, required=True)
    repack_parser.add_argument("--shim", type=Path, default=DEFAULT_SHIM_PATH)

    args = parser.parse_args(argv)

    if args.command == "discover":
        artifacts = discover(query_pypi())
        text = _write_matrix(artifacts, args.matrix_file)
        print(text)
        for artifact in artifacts:
            print(
                f"-> {artifact.platform}: {artifact.version} ({artifact.filename})",
                file=sys.stderr,
            )
        return 0

    repack_wheel(args.wheel.resolve(), args.out_dir.resolve(), args.shim.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
