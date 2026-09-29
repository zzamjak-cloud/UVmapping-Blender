"""Blender 없이 실행하는 OpenRouter API 키 백업 저장소 테스트.

업데이트·재설치로 환경설정 키가 지워져도 백업에서 되살아나야 하고, 사용자가
직접 키를 지웠을 때는 백업도 함께 사라져야 한다.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.api_key_store import key_to_restore, read_key, write_key


def test_write_then_read_round_trip() -> None:
    with tempfile.TemporaryDirectory(prefix="uvmapping-key-") as temp_dir:
        path = Path(temp_dir) / "nested" / "openrouter_api_key"
        write_key(path, "  sk-or-test  ")
        assert read_key(path) == "sk-or-test"
        # 임시 파일이 남지 않아야 한다.
        assert sorted(p.name for p in path.parent.iterdir()) == ["openrouter_api_key"]


def test_empty_key_removes_backup() -> None:
    with tempfile.TemporaryDirectory(prefix="uvmapping-key-") as temp_dir:
        path = Path(temp_dir) / "openrouter_api_key"
        write_key(path, "sk-or-test")
        write_key(path, "   ")
        assert not path.exists()
        # 없는 파일을 다시 지워도 오류가 나지 않아야 한다.
        write_key(path, "")
        assert read_key(path) == ""


def test_missing_backup_reads_empty() -> None:
    with tempfile.TemporaryDirectory(prefix="uvmapping-key-") as temp_dir:
        assert read_key(Path(temp_dir) / "missing") == ""


def test_restore_only_when_preference_is_empty() -> None:
    assert key_to_restore("", "sk-or-backup") == "sk-or-backup"
    assert key_to_restore("  ", " sk-or-backup ") == "sk-or-backup"
    # 사용자가 새 키를 입력해 두었다면 백업으로 덮어쓰지 않는다.
    assert key_to_restore("sk-or-current", "sk-or-backup") == ""
    assert key_to_restore("", "") == ""


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"API 키 백업 순수 테스트 {len(tests)}/{len(tests)} 통과")
