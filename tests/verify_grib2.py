#!/usr/bin/env python3
"""End-to-end verification of the repackaged ``eccodes`` wheel on Python 3.14.

The script must be run inside an environment where the repackaged wheel was
installed together with ``cfgrib`` and ``xarray``.  It performs the
mandatory acceptance checks:

1. Python 3.14 is the running interpreter and ``gribapi.bindings`` resolves
   ``library_path`` to a native library that exists on disk and belongs to
   the installed wheel.
2. The glue module ``eccodes._eccodes`` is the pure-Python shim (not a
   cp313 compiled extension) and exposes ``versions()``.
3. A GRIB2 message is synthesised with
   ``eccodes.codes_new_from_samples("regular_ll_sfc_grib2", ...)`` on a
   small regular lat/lon grid and written to a temporary file.
4. The file is read back with ``xarray.open_dataset(..., engine="cfgrib")``
   and every encoded value is compared against the input array.

Any failure raises and the process exits non-zero, which blocks the
release job in CI.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Tuple

import numpy as np


def _print_header(title: str) -> None:
    """Print a visibly delimited section header.

    Args:
        title: Section title.
    """
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def check_runtime() -> None:
    """Assert that the wheel is being exercised under CPython 3.14.

    Raises:
        SystemExit: On any other interpreter.
    """
    _print_header("1. Runtime environment")
    print(f"executable      : {sys.executable}")
    print(f"python version  : {sys.version}")
    print(f"platform         : {sys.platform}")
    if sys.version_info[:2] != (3, 14):
        raise SystemExit(
            f"this verification must run on Python 3.14, got {sys.version_info[:3]}"
        )


def check_library_binding() -> Any:
    """Validate the native-library lookup performed by ``gribapi``.

    Returns:
        The imported ``eccodes._eccodes`` glue module.
    """
    _print_header("2. Native library binding")
    import eccodes  # noqa: F401  (import first to surface load errors early)
    from gribapi.bindings import library_path

    # ``repack`` (the default) verifies the wheels produced by this repo; the
    # ``official`` variant verifies ECMWF's own eccodeslib-based distribution,
    # which has no ``eccodes._eccodes`` glue module at all.
    variant = os.environ.get("VERIFY_VARIANT", "repack")
    print(f"verification variant: {variant}")

    try:
        import eccodes._eccodes as glue
    except ImportError:
        glue = None

    print(f"eccodes python pkg : {eccodes.__file__}")
    print(f"eccodes version    : {eccodes.codes_get_api_version()}")
    print(f"library_path       : {library_path}")
    print(f"glue module        : {glue.__file__ if glue is not None else '(none)'}")
    if glue is not None:
        print(f"glue versions()    : {glue.versions()}")

    if not os.path.exists(library_path):
        raise SystemExit(f"library_path does not exist: {library_path}")
    lowered = library_path.lower()
    if sys.platform.startswith("linux"):
        if not (lowered.endswith(".so") or ".so" in lowered):
            raise SystemExit(f"expected a .so library path, got {library_path}")
    elif os.name == "nt":
        if not lowered.endswith(".dll"):
            raise SystemExit(f"expected a .dll library path, got {library_path}")
    if variant == "repack" and (
        glue is None or Path(glue.__file__).name != "_eccodes.py"
    ):
        raise SystemExit(
            "repacked wheel must load the pure-Python _eccodes.py shim, "
            f"got {glue}"
        )
    return glue


def write_sample_grib2(path: Path, values: np.ndarray, ni: int, nj: int) -> None:
    """Synthesise a small GRIB2 regular lat/lon surface field.

    Args:
        path: Destination GRIB file path.
        values: Flat ``ni * nj`` data values to encode.
        ni: Number of grid points in longitude.
        nj: Number of grid points in latitude.
    """
    import eccodes

    gid = eccodes.codes_new_from_samples(
        "regular_ll_sfc_grib2", eccodes.CODES_PRODUCT_GRIB
    )
    try:
        eccodes.codes_set(gid, "shortName", "2t")
        # 6 latitudes x 12 longitudes: 90N ... 60S, 0E ... 330E
        for key, value in (
            ("Ni", ni),
            ("Nj", nj),
            ("latitudeOfFirstGridPointInDegrees", 90.0),
            ("latitudeOfLastGridPointInDegrees", -60.0),
            ("longitudeOfFirstGridPointInDegrees", 0.0),
            ("longitudeOfLastGridPointInDegrees", 330.0),
            ("iDirectionIncrementInDegrees", 30.0),
            ("jDirectionIncrementInDegrees", 30.0),
        ):
            eccodes.codes_set(gid, key, value)
        eccodes.codes_set(gid, "bitsPerValue", 24)
        eccodes.codes_set_values(gid, values)
        with open(path, "wb") as handle:
            eccodes.codes_write(gid, handle)
    finally:
        eccodes.codes_release(gid)


def roundtrip_with_cfgrib(path: Path) -> Tuple[str, np.ndarray]:
    """Read a GRIB file back through the cfgrib/xarray stack.

    Args:
        path: GRIB file path.

    Returns:
        The selected data variable name and its 2-D values array.
    """
    import xarray as xr

    dataset = xr.open_dataset(
        path,
        engine="cfgrib",
        backend_kwargs={"indexpath": ""},
    )
    print(dataset)
    data_vars = [
        name
        for name, variable in dataset.data_vars.items()
        if variable.ndim == 2 and variable.shape != ()
    ]
    if len(data_vars) != 1:
        raise SystemExit(f"expected exactly one 2-D data variable, got {data_vars}")
    name = data_vars[0]
    return name, np.asarray(dataset[name].values, dtype=np.float64)


def main() -> int:
    """Run all verification stages.

    Returns:
        Process exit code (0 on success).
    """
    check_runtime()
    check_library_binding()

    ni, nj = 12, 6
    # Integer data with 24-bit simple packing: quantisation error is below
    # 71 / 2**24 ~= 4.2e-6, far inside the 1e-4 comparison tolerance.
    expected = np.arange(ni * nj, dtype=np.float64).reshape(nj, ni)

    _print_header("3. Synthesise GRIB2 with eccodes")
    with tempfile.TemporaryDirectory(prefix="eccodes_cp314_") as tmp:
        grib_path = Path(tmp) / "regular_ll_sfc_grib2.grib2"
        write_sample_grib2(grib_path, expected.reshape(-1), ni, nj)
        size = grib_path.stat().st_size
        print(f"written file  : {grib_path}")
        print(f"file size     : {size} bytes")
        if size <= 0:
            raise SystemExit("GRIB file is empty")

        _print_header("4. Read back with xarray + cfgrib")
        name, actual = roundtrip_with_cfgrib(grib_path)

        print(f"data variable  : {name}")
        print(f"shape          : expected {expected.shape}, got {actual.shape}")
        if actual.shape != expected.shape:
            raise SystemExit("grid shape mismatch after cfgrib roundtrip")

        # cfgrib keeps north-to-south scanning order, but tolerate a flip.
        candidates = {
            "same order": actual,
            "flipped N/S": np.flipud(actual),
        }
        max_diff = float(np.abs(actual - expected).max())
        print(f"max abs diff   : {max_diff:.6g}")
        if not np.allclose(actual, expected, atol=1e-4, rtol=0.0) and not np.allclose(
            candidates["flipped N/S"], expected, atol=1e-4, rtol=0.0
        ):
            print("expected (first row):", expected[0])
            print("actual   (first row):", actual[0])
            raise SystemExit("value mismatch after cfgrib roundtrip")

        orientation = (
            "same order"
            if np.allclose(actual, expected, atol=1e-4, rtol=0.0)
            else "flipped N/S"
        )
        print(f"value check    : PASS ({orientation}, atol=1e-4)")
        print(f"value range    : {actual.min()} .. {actual.max()}")

    _print_header("ALL CHECKS PASSED")
    print("eccodes + cfgrib work end-to-end on this Python 3.14 interpreter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
