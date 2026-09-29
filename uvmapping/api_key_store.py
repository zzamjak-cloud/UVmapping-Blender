"""OpenRouter API 키 백업 파일 저장소.

Blender는 애드온 등록이 한 번이라도 실패하거나 제거 후 다시 설치되면 사용자
환경설정의 애드온 항목을 지우고, 그 안의 API 키도 함께 사라진다. 키를 Blender
사용자 설정 폴더의 별도 파일에도 기록해 두고 환경설정 값이 비었을 때 되살린다.
이 모듈은 ``bpy`` 없이 테스트할 수 있도록 경로를 인자로만 받는다.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


# Blender 사용자 설정 폴더(CONFIG) 아래에 만드는 백업 위치. Extension 저장소 이름과
# 무관하게 고정해 두어야 재설치·저장소 변경 뒤에도 같은 파일을 찾는다.
BACKUP_DIRECTORY_NAME = "uvmapping_blender"
BACKUP_FILE_NAME = "openrouter_api_key"


def read_key(path: Path) -> str:
    """백업 파일의 키를 읽는다. 파일이 없거나 읽을 수 없으면 빈 문자열을 반환한다."""

    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_key(path: Path, api_key: str) -> None:
    """키를 백업 파일에 원자적으로 기록한다. 빈 키면 백업 파일을 지운다."""

    path = Path(path)
    api_key = api_key.strip()
    if not api_key:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if read_key(path) == api_key:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp는 고유 이름과 소유자 전용(0600) 권한으로 만들어 동시 쓰기와 노출을 막는다.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(api_key)
        os.replace(temporary, path)
    except BaseException:
        # 실패해도 키가 담긴 임시 파일을 남기지 않는다.
        temporary.unlink(missing_ok=True)
        raise


def key_to_restore(current_key: str, backup_key: str) -> str:
    """환경설정 값이 비었고 백업이 있을 때만 되살릴 키를, 아니면 빈 문자열을 반환한다."""

    if current_key.strip():
        return ""
    return backup_key.strip()


__all__ = (
    "BACKUP_DIRECTORY_NAME",
    "BACKUP_FILE_NAME",
    "key_to_restore",
    "read_key",
    "write_key",
)
