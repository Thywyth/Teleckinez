"""Check that key runtime dependencies import and report their versions.

Usage: python scripts/check_deps.py
Exit code is non-zero if any required module fails to import.
"""

import importlib
import importlib.metadata as md
import platform
import sys

from teleckinez.voice.cuda import register_cuda_dlls

# (import name, distribution name)
MODULES = [
    ("aiomqtt", "aiomqtt"),
    ("numpy", "numpy"),
    ("sounddevice", "sounddevice"),
    ("webrtcvad", "webrtcvad-wheels"),
    ("vosk", "vosk"),
    ("faster_whisper", "faster-whisper"),
    ("ctranslate2", "ctranslate2"),
    ("piper", "piper-tts"),
    ("onnxruntime", "onnxruntime"),
    ("openwakeword", "openwakeword"),
    ("pyttsx3", "pyttsx3"),
    ("pynput", "pynput"),
    ("ollama", "ollama"),
]


def _version(dist: str) -> str:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return "?"


def _cuda_libs() -> str:
    """Report whether cuBLAS/cuDNN DLLs needed by faster-whisper on GPU can be loaded."""
    if sys.platform != "win32":
        return "n/a"
    import ctypes

    missing = []
    for dll in ("cublas64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.WinDLL(dll)
        except OSError:
            missing.append(dll)
    return "ok" if not missing else "missing:" + ",".join(missing)


def _extra_info(name: str, module: object) -> str:
    if name == "ctranslate2":
        cuda = module.get_cuda_device_count()
        types = sorted(module.get_supported_compute_types("cuda")) if cuda else []
        return f"cuda_devices={cuda} cuda_types={','.join(types) or '-'} libs={_cuda_libs()}"
    if name == "onnxruntime":
        return "providers=" + ",".join(module.get_available_providers())
    if name == "sounddevice":
        default_in, default_out = module.default.device
        count = len(module.query_devices())
        return f"devices={count} default_in={default_in} default_out={default_out}"
    return ""


def main() -> int:
    print(f"Python {sys.version.split()[0]} on {platform.system()} {platform.release()}")
    register_cuda_dlls()
    failed = 0
    for name, dist in MODULES:
        try:
            module = importlib.import_module(name)
            info = _extra_info(name, module)
            print(f"  OK    {name:<15} {_version(dist):<12} {info}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name:<15} {_version(dist):<12} {type(exc).__name__}: {exc}")
    print("all OK" if not failed else f"{failed} module(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
