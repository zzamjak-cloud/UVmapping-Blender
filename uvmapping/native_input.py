"""Blender의 한글 IME 문제를 우회하는 운영체제 네이티브 텍스트 입력창."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable


log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.2
_DIALOG_TIMEOUT = 60 * 60
_PROCESS_STOP_TIMEOUT = 1.0
_state = {
    "proc": None,
    "on_done": None,
    "dir": None,
    "result": None,
    "started": None,
    "timer_registered": False,
}

# osascript 자체가 아니라 System Events를 활성화해야 Blender 뒤에 다이얼로그가
# 숨지 않는다. 최초 사용 때 macOS 자동화 권한 안내가 나타날 수 있다.
_APPLESCRIPT = '''on run argv
    set dialogTitle to item 1 of argv
    set initPath to item 2 of argv
    set resultPath to item 3 of argv
    set initText to ""
    try
        set initText to (read POSIX file initPath as «class utf8»)
    end try
    tell application "System Events"
        activate
        set d to display dialog dialogTitle default answer initText buttons {"취소", "입력완료"} default button "입력완료" cancel button "취소" with title dialogTitle
    end tell
    set out to open for access POSIX file resultPath with write permission
    set eof of out to 0
    write (text returned of d) to out as «class utf8»
    close access out
end run'''

# Windows PowerShell 5.1에서 한글이 깨지지 않도록 호출부에서 UTF-8 BOM으로 쓴다.
# stdout 대신 결과 파일을 사용해 콘솔 코드페이지와 따옴표 이스케이프를 피한다.
_POWERSHELL = '''Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$title = $args[0]
$initPath = $args[1]
$resultPath = $args[2]
$utf8 = New-Object System.Text.UTF8Encoding($false)
$init = ''
if (Test-Path -LiteralPath $initPath) {
    $init = [System.IO.File]::ReadAllText($initPath, $utf8)
}
$form = New-Object System.Windows.Forms.Form
$form.Text = $title
$form.Width = 640
$form.Height = 280
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$box = New-Object System.Windows.Forms.TextBox
$box.Multiline = $true
$box.AcceptsReturn = $true
$box.ScrollBars = 'Vertical'
$box.Text = $init
$box.SetBounds(12, 12, 600, 158)
$box.Anchor = 'Top,Left,Right,Bottom'
$ok = New-Object System.Windows.Forms.Button
$ok.Text = '입력완료'
$ok.DialogResult = 'OK'
$ok.SetBounds(410, 188, 98, 32)
$ok.Anchor = 'Bottom,Right'
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = '취소'
$cancel.DialogResult = 'Cancel'
$cancel.SetBounds(514, 188, 98, 32)
$cancel.Anchor = 'Bottom,Right'
$form.Controls.Add($box)
$form.Controls.Add($ok)
$form.Controls.Add($cancel)
$form.AcceptButton = $ok
$form.CancelButton = $cancel
$form.Add_Shown({ $form.Activate(); $box.Focus(); $box.SelectAll() })
if ($form.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [System.IO.File]::WriteAllText($resultPath, $box.Text, $utf8)
}
$form.Dispose()'''


def is_supported(platform: str | None = None) -> bool:
    """현재 또는 지정한 플랫폼에서 네이티브 입력창을 지원하는지 반환한다."""

    return (platform or sys.platform) in {"darwin", "win32"}


def is_open() -> bool:
    """네이티브 입력창 프로세스가 열려 있는지 반환한다."""

    return _state["proc"] is not None


def to_single_line(text: str | None) -> str:
    """개행과 연속 공백을 Blender ``StringProperty``용 한 줄로 정규화한다."""

    return " ".join((text or "").split())


def build_command(
    platform: str,
    work_dir: str,
    title: str,
    init_path: str,
    result_path: str,
) -> tuple[list[str] | None, int]:
    """플랫폼별 안전한 인자 리스트와 Windows 생성 플래그를 만든다."""

    if platform == "darwin":
        return ["osascript", "-e", _APPLESCRIPT, title, init_path, result_path], 0
    if platform == "win32":
        script_path = os.path.join(work_dir, f"uvmapping_dialog_{uuid.uuid4().hex}.ps1")
        with open(script_path, "w", encoding="utf-8-sig", newline="\n") as handle:
            handle.write(_POWERSHELL)
        command = [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
            title,
            init_path,
            result_path,
        ]
        return command, getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return None, 0


def open_dialog(
    title: str,
    initial_text: str,
    on_done: Callable[[str | None], None],
) -> str | None:
    """입력창을 비동기로 열고 완료 후 Blender 메인 스레드에서 콜백한다.

    콜백의 값이 ``None``이면 사용자가 취소했거나 제한 시간이 지난 것이다.
    정상 시작 시 ``None``, 시작할 수 없으면 한국어 오류 메시지를 반환한다.
    """

    if not is_supported():
        return "이 플랫폼에서는 네이티브 입력 창을 지원하지 않습니다"
    if is_open():
        return "이미 입력 창이 열려 있습니다"

    import bpy

    work_dir = tempfile.mkdtemp(prefix="uvmapping_input_")
    init_path = os.path.join(work_dir, "initial.txt")
    result_path = os.path.join(work_dir, "result.txt")
    try:
        with open(init_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(initial_text or "")
        command, creationflags = build_command(
            sys.platform,
            work_dir,
            title,
            init_path,
            result_path,
        )
        if command is None:
            raise RuntimeError("지원하지 않는 플랫폼입니다")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        _state.update(
            proc=process,
            on_done=on_done,
            dir=work_dir,
            result=result_path,
            started=time.monotonic(),
            timer_registered=False,
        )
        bpy.app.timers.register(
            _poll,
            first_interval=_POLL_INTERVAL,
            persistent=True,
        )
        _state["timer_registered"] = True
    except Exception as error:
        _stop_process(_state.get("proc"))
        shutil.rmtree(work_dir, ignore_errors=True)
        _reset_state()
        return f"입력 창 실행 실패: {error}"
    return None


def _stop_process(process) -> None:
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=_PROCESS_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_PROCESS_STOP_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        log.warning("네이티브 입력 프로세스를 종료하지 못했습니다", exc_info=True)


def _reset_state() -> None:
    _state.update(
        proc=None,
        on_done=None,
        dir=None,
        result=None,
        started=None,
        timer_registered=False,
    )


def _poll():
    process = _state["proc"]
    if process is None:
        _state["timer_registered"] = False
        return None

    started = _state["started"]
    timed_out = bool(started is not None and time.monotonic() - started >= _DIALOG_TIMEOUT)
    if process.poll() is None and not timed_out:
        return _POLL_INTERVAL
    if timed_out:
        log.warning("네이티브 입력 창 제한 시간이 지나 자동으로 닫습니다")
        _stop_process(process)

    callback = _state["on_done"]
    result_path = _state["result"]
    work_dir = _state["dir"]
    text = None
    if not timed_out and result_path:
        try:
            with open(result_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = None

    _reset_state()
    shutil.rmtree(work_dir, ignore_errors=True)
    if callback is not None:
        try:
            callback(text)
        except Exception:
            log.exception("네이티브 입력 완료 콜백 오류")
    _redraw_view3d()
    return None


def _unregister_timer() -> None:
    if not _state["timer_registered"]:
        return
    try:
        import bpy

        timers = bpy.app.timers
        if not hasattr(timers, "is_registered") or timers.is_registered(_poll):
            timers.unregister(_poll)
    except Exception:
        # Blender 종료 중에는 타이머 레지스트리가 이미 해제됐을 수 있다.
        log.debug("네이티브 입력 타이머 정리를 건너뜁니다", exc_info=True)


def shutdown() -> None:
    """애드온 해제 시 입력 프로세스, 타이머와 임시 파일을 모두 정리한다."""

    process = _state["proc"]
    work_dir = _state["dir"]
    _unregister_timer()
    _stop_process(process)
    _reset_state()
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)


def _redraw_view3d() -> None:
    try:
        import bpy

        window_manager = bpy.context.window_manager
        if not window_manager:
            return
        for window in window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        log.debug("3D 뷰 갱신을 건너뜁니다", exc_info=True)
