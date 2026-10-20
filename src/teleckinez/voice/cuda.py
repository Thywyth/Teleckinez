"""Make CUDA runtime DLLs from the nvidia-* pip wheels visible on Windows.

faster-whisper (CTranslate2) needs cuBLAS 12 and cuDNN 9 at runtime. Instead of
installing the full CUDA Toolkit we use the `nvidia-cublas-cu12` and
`nvidia-cudnn-cu12` wheels (extra `cuda`) and add their `bin` folders to the
DLL search path. Must be called before the first CUDA model is loaded.
"""

import os
import sys
from pathlib import Path

_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc")
_registered: list[Path] = []


def register_cuda_dlls() -> list[Path]:
    """Add nvidia wheel DLL folders to the search path; return the folders added."""
    if sys.platform != "win32" or _registered:
        return list(_registered)
    try:
        import nvidia  # namespace package installed by the nvidia-* wheels
    except ImportError:
        return []
    for root in map(Path, nvidia.__path__):
        for name in _PACKAGES:
            bin_dir = root / name / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                # CTranslate2 loads some libraries lazily via PATH lookup.
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                _registered.append(bin_dir)
    return list(_registered)
