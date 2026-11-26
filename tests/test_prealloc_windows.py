"""Windows에서 큰 파일 미리 잡기가 0 채우기 없이 바로 끝나는지 (40GB '연결 중' 지연 원인)."""
import os
import sys
import time

import pytest

from drivepull.engine import preallocate

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 전용")


def test_prealloc_40gb_instant(tmp_path):
    p = str(tmp_path / "big.mov.drivepull")
    size = 40 * 1024 ** 3
    t0 = time.monotonic()
    sparse = preallocate(p, size)
    took = time.monotonic() - t0
    try:
        assert os.path.getsize(p) == size
        assert sparse, "NTFS인데 sparse 지정 실패"
        assert took < 3.0, f"미리 잡기에 {took:.1f}초 걸림"
        # 파일 끝 근처에 써도 앞부분 0 채우기로 멈추지 않아야 함
        t0 = time.monotonic()
        with open(p, "r+b", buffering=0) as f:
            f.seek(size - 1024 * 1024)
            f.write(b"x" * 1024 * 1024)
        assert time.monotonic() - t0 < 3.0
    finally:
        os.remove(p)


def test_disk_type_query(tmp_path):
    from drivepull.engine import disk_is_hdd
    r = disk_is_hdd(str(tmp_path / "x.mov"))
    print("C: seek penalty (HDD) =", r)
    assert r in (True, False)   # Windows 로컬 드라이브면 판별 가능해야 함
