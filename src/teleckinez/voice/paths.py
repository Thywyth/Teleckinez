"""Model paths that native libraries can open on Windows.

Vosk (Kaldi) opens model files with narrow-char C++ APIs, so on Windows an
absolute path with non-ASCII characters (e.g. a Cyrillic folder name) fails
with "Failed to create a model". We pass it an ASCII-only equivalent instead:
a path relative to the current directory, or the 8.3 short path.
"""

import os
import sys
from pathlib import Path


def _short_path(path: Path) -> str | None:
    import ctypes

    buf = ctypes.create_unicode_buffer(32_768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
    return buf.value if 0 < n < len(buf) else None


def ascii_safe_path(path: str | os.PathLike[str]) -> str:
    """Return an ASCII-only path to an existing file/folder (Windows), else the path itself."""
    resolved = Path(path).resolve()
    if sys.platform != "win32" or str(resolved).isascii():
        return str(resolved)
    try:
        relative = os.path.relpath(resolved)
    except ValueError:  # different drive
        relative = ""
    if relative and relative.isascii():
        return relative
    short = _short_path(resolved)
    if short and short.isascii():
        return short
    raise FileNotFoundError(
        f"cannot build an ASCII-only path to {resolved}; move the project to a folder "
        "without non-Latin characters or run from the project root"
    )
