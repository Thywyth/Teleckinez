import sys

from teleckinez.voice import cuda


def test_register_is_idempotent() -> None:
    first = cuda.register_cuda_dlls()
    second = cuda.register_cuda_dlls()
    assert first == second
    if sys.platform != "win32":
        assert first == []
