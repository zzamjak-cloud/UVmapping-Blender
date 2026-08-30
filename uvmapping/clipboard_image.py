"""운영체제 클립보드의 이미지를 PNG 참조 파일로 저장한다.

Blender는 이미지 클립보드를 직접 제공하지 않으므로 운영체제 기본 도구를
사용한다. 이 모듈은 ``bpy``에 의존하지 않아 순수 파이썬으로 검사할 수 있다.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


log = logging.getLogger(__name__)

_COMMAND_TIMEOUT = 15
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# 경로는 스크립트 문자열에 삽입하지 않고 argv로 전달해 따옴표와 공백을 안전하게
# 처리한다. PNG가 없는 일부 애플리케이션을 위해 TIFF도 차선으로 시도한다.
_APPLESCRIPT = '''on run argv
    set outPath to item 1 of argv
    set tiffPath to item 2 of argv
    try
        set imgData to the clipboard as «class PNGf»
        set fp to open for access POSIX file outPath with write permission
        set eof of fp to 0
        write imgData to fp
        close access fp
        return "PNG"
    end try
    try
        set imgData to the clipboard as TIFF picture
        set fp to open for access POSIX file tiffPath with write permission
        set eof of fp to 0
        write imgData to fp
        close access fp
        return "TIFF"
    end try
    return "NOIMAGE"
end run'''

# Clipboard.GetImage는 STA 스레드에서만 동작한다. PowerShell 5.1에서 한글 경로를
# 안전하게 읽도록 호출부에서 이 스크립트를 UTF-8 BOM으로 기록한다.
_POWERSHELL = '''Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$outPath = $args[0]
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($img -ne $null) {
    try {
        $img.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
        Write-Output "PNG"
    } finally {
        $img.Dispose()
    }
} else {
    Write-Output "NOIMAGE"
}'''


def is_supported(platform: str | None = None) -> bool:
    """현재 또는 지정한 플랫폼이 이미지 붙여넣기를 지원하는지 반환한다."""

    return (platform or sys.platform) in {"darwin", "win32"}


def target_path(directory: str) -> str:
    """동시 호출끼리 겹치지 않는 PNG 저장 경로를 만든다."""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    token = uuid.uuid4().hex
    return os.path.join(directory, f"uvmapping_ref_clipboard_{stamp}_{token}.png")


def _run(command: list[str], *, cwd: str | None = None, creationflags: int = 0) -> str:
    """셸을 거치지 않고 제한 시간 안에서 명령을 실행해 표준 출력을 반환한다."""

    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("클립보드 이미지 명령 실행 실패")
        return ""
    if completed.returncode != 0:
        log.warning("클립보드 이미지 명령이 실패했습니다: 종료 코드 %s", completed.returncode)
        return ""
    return (completed.stdout or "").strip()


def _remove(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("임시 클립보드 파일을 정리하지 못했습니다: %s", path)


def _is_png(path: str) -> bool:
    try:
        if os.path.getsize(path) <= len(_PNG_SIGNATURE):
            return False
        with open(path, "rb") as handle:
            return handle.read(len(_PNG_SIGNATURE)) == _PNG_SIGNATURE
    except OSError:
        return False


def paste_to(directory: str, platform: str | None = None) -> tuple[str | None, str | None]:
    """클립보드 이미지를 ``directory``에 PNG로 저장한다.

    성공하면 ``(경로, None)``, 실패하면 ``(None, 한국어 오류 메시지)``를
    반환한다. 생성 도중 실패한 출력과 임시 스크립트는 항상 정리한다.
    """

    current_platform = platform or sys.platform
    if not is_supported(current_platform):
        return None, "이 플랫폼에서는 클립보드 이미지 붙여넣기를 지원하지 않습니다"
    if not os.path.isdir(directory):
        return None, f"저장할 폴더가 없습니다: {directory}"

    output_path = target_path(directory)
    temporary_paths: list[str] = []
    temporary_directories: list[str] = []
    try:
        if current_platform == "darwin":
            tiff_path = os.path.splitext(output_path)[0] + ".tiff"
            temporary_paths.append(tiff_path)
            result = _run(["osascript", "-e", _APPLESCRIPT, output_path, tiff_path])
            if result == "TIFF" and os.path.isfile(tiff_path):
                _run(["sips", "-s", "format", "png", tiff_path, "--out", output_path])
        else:
            try:
                script_directory = tempfile.mkdtemp(prefix="uvmapping_clipboard_")
            except OSError:
                return None, "클립보드용 임시 폴더를 만들지 못했습니다"
            temporary_directories.append(script_directory)
            script_path = os.path.join(
                script_directory,
                f"uvmapping_clipboard_{uuid.uuid4().hex}.ps1",
            )
            temporary_paths.append(script_path)
            try:
                with open(script_path, "w", encoding="utf-8-sig", newline="\n") as handle:
                    handle.write(_POWERSHELL)
            except OSError:
                return None, "클립보드용 임시 스크립트를 만들지 못했습니다"
            _run(
                [
                    "powershell",
                    "-NoLogo",
                    "-NoProfile",
                    "-STA",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    script_path,
                    output_path,
                ],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        if _is_png(output_path):
            return output_path, None
        _remove(output_path)
        return None, (
            "클립보드에 이미지가 없습니다. "
            "브라우저에서 '이미지 복사'로 복사했는지 확인하세요"
        )
    finally:
        for temporary_path in temporary_paths:
            _remove(temporary_path)
        for temporary_directory in temporary_directories:
            shutil.rmtree(temporary_directory, ignore_errors=True)
