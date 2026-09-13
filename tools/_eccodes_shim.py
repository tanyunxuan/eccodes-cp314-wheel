"""Pure-Python drop-in replacement for ECMWF's compiled ``eccodes._eccodes``.

Background
----------
The official ``eccodes`` binary wheels ship a tiny hand-written CPython
extension (``eccodes/_eccodes.cp313-win_amd64.pyd`` on Windows and
``eccodes/_eccodes.cpython-313-*.so`` on Linux), whose C source
(``eccodes/_eccodes.cc``) does exactly two things:

1. link against ``libeccodes`` at C level so that, when the extension is
   imported, the OS dynamic loader resolves all the bundled native
   libraries (the Windows DLL dependency graph, or the auditwheel-hashed
   sibling libraries inside ``eccodes.libs`` on Linux);
2. expose ``versions() -> {"eccodes": ECCODES_VERSION_STR}``.

The compiled extension carries the ``cp313`` ABI tag and therefore cannot
be imported by Python 3.14.  The actual ecCodes native libraries on the
other hand expose a stable C ABI that is independent of the CPython
version.  This module reproduces behaviour (1) with :mod:`ctypes` and
behaviour (2) from installed distribution metadata.  It requires no C
compilation and no other upstream ``eccodes`` Python source file is
modified -- the only change inside the repackaged wheel is that this file
takes the place of the old compiled extension.
"""

from __future__ import annotations

import ctypes
import glob
import importlib.metadata
import os
import sys
from typing import Dict, List

_HERE: str = os.path.dirname(os.path.abspath(__file__))

# ``LOAD_LIBRARY_SEARCH_DEFAULT_DIRS``: honours directories registered with
# ``os.add_dll_directory`` plus the application and system directories.
_WIN_LOAD_LIBRARY_SEARCH_DEFAULT_DIRS: int = 0x00001000


def _eccodes_version() -> str:
    """Return the version of the installed ``eccodes`` distribution.

    Returns:
        The distribution version string (for example ``"2.48.0"``), or
        ``"unknown"`` if distribution metadata cannot be located.
    """
    try:
        return importlib.metadata.version("eccodes")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _load_with_retry(paths: List[str], load) -> None:
    """Load libraries, retrying failed ones in case of load-order deps.

    Args:
        paths: Candidate library file paths (missing ones are skipped).
        load: One-argument callable that loads a single path and raises
            :class:`OSError` on failure.

    Raises:
        OSError: If at least one existing library cannot be loaded after
            no further progress is possible.
    """
    pending = [p for p in paths if os.path.exists(p)]
    while pending:
        remaining: List[str] = []
        made_progress = False
        for path in pending:
            try:
                load(path)
                made_progress = True
            except OSError:
                remaining.append(path)
        if not made_progress:
            # Final attempt: surface the real loader error to the caller.
            load(remaining[0])
        pending = remaining


def _preload_windows() -> None:
    """Preload every bundled DLL from the package directory on Windows.

    The dependency DLLs shipped next to ``eccodes.dll`` (``eccodes_memfs``,
    ``aec``, ``zlib``, ``libpng16``, ``openjp2``) must be resolvable before
    cffi dlopens ``eccodes.dll``.  Registering the package directory with
    :func:`os.add_dll_directory` and preloading the libraries in a
    dependency-friendly order replicates what importing the compiled
    extension achieves through its import table.

    Raises:
        ImportError: If ``eccodes.dll`` is not present in the wheel.
    """
    package_dir = _HERE
    os.add_dll_directory(package_dir)

    preferred = (
        "z.dll",
        "aec.dll",
        "libpng16.dll",
        "openjp2.dll",
        "eccodes_memfs.dll",
        "eccodes.dll",
    )
    ordered = [os.path.join(package_dir, name) for name in preferred]
    for path in sorted(glob.glob(os.path.join(package_dir, "*.dll"))):
        if path not in ordered:
            ordered.append(path)

    def _load(path: str) -> None:
        ctypes.CDLL(path, winmode=_WIN_LOAD_LIBRARY_SEARCH_DEFAULT_DIRS)

    _load_with_retry(ordered, _load)

    eccodes_dll = os.path.join(package_dir, "eccodes.dll")
    if not os.path.exists(eccodes_dll):
        raise ImportError("bundled eccodes.dll is missing from the wheel")
    ctypes.CDLL(eccodes_dll, winmode=_WIN_LOAD_LIBRARY_SEARCH_DEFAULT_DIRS)


def _preload_linux() -> None:
    """Preload the auditwheel-bundled shared libraries on Linux.

    The manylinux wheel stores its libraries in ``eccodes.libs`` with
    hashed SONAMEs (for example ``libeccodes-bce07ef4.so``).  They are
    loaded globally (``RTLD_GLOBAL``) so that the subsequent cffi
    ``dlopen`` performed by ``gribapi.bindings`` resolves every dependency.

    Raises:
        ImportError: If the ``eccodes.libs`` directory or the main
            ``libeccodes`` shared object is missing from the wheel.
    """
    libs_dir = os.path.abspath(os.path.join(_HERE, os.pardir, "eccodes.libs"))
    if not os.path.isdir(libs_dir):
        raise ImportError(f"bundled eccodes.libs directory not found: {libs_dir}")

    candidates = glob.glob(os.path.join(libs_dir, "*.so*"))
    # Versioned dependency SONAMEs (libaec-....so.0.0.8, ...) load first;
    # the plain ``.so`` entry points (libeccodes itself) are loaded later.
    candidates.sort(key=lambda p: (p.endswith(".so"), p))

    def _load(path: str) -> None:
        ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)

    _load_with_retry(candidates, _load)

    mains = [
        p
        for p in glob.glob(os.path.join(libs_dir, "libeccodes-*.so"))
        if "memfs" not in p
    ]
    if not mains:
        raise ImportError("bundled libeccodes shared object is missing")
    _load(mains[0])


if os.name == "nt":
    _preload_windows()
elif sys.platform.startswith("linux"):
    _preload_linux()
else:  # pragma: no cover - the repackaged wheels only target Win/Linux
    raise ImportError(
        "the repackaged eccodes cp314 wheel supports Windows and Linux only"
    )


def versions() -> Dict[str, str]:
    """Return version information, mimicking the upstream C extension.

    Returns:
        A mapping with the single key ``"eccodes"`` and the bundled
        ecCodes library version as its value.
    """
    return {"eccodes": _eccodes_version()}
