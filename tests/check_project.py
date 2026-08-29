"""Blender를 실행하지 않고 프로젝트 구조와 개발 실행기를 검사합니다."""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
ADDON_ID = "uvmapping_blender"
PROFILE_NAME = "UVmappingBlenderDev"
REQUIRED_FILES = (
    "blender_manifest.toml",
    "__init__.py",
    "scripts/dev_run.sh",
    "scripts/dev_run.ps1",
    "scripts/dev_run.bat",
    "scripts/dev_bootstrap.py",
    "tests/blender_smoke.py",
    "tests/blender_quality.py",
    "tests/test_analysis_pure.py",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _read(relative_path: str, encoding: str = "utf-8") -> str:
    return (ROOT / relative_path).read_text(encoding=encoding)


def _check_required_files() -> None:
    missing = [path for path in REQUIRED_FILES if not (ROOT / path).is_file()]
    _require(not missing, f"필수 파일이 없습니다: {', '.join(missing)}")


def _check_manifest() -> str:
    with (ROOT / "blender_manifest.toml").open("rb") as manifest_file:
        manifest = tomllib.load(manifest_file)
    _require(manifest.get("schema_version") == "1.0.0", "schema_version은 1.0.0이어야 합니다.")
    _require(manifest.get("id") == ADDON_ID, f"매니페스트 id는 {ADDON_ID}여야 합니다.")
    version = str(manifest.get("version", ""))
    _require(bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), f"유효하지 않은 버전입니다: {version!r}")
    excluded = set(manifest.get("build", {}).get("paths_exclude_pattern", ()))
    for required_pattern in ("/tests/", "/scripts/", "/.git/", "__pycache__/"):
        _require(
            required_pattern in excluded,
            f"패키지 제외 패턴이 없습니다: {required_pattern}",
        )
    return version


def _check_operator_source() -> None:
    source_files = [
        path
        for path in ROOT.rglob("*.py")
        if "tests" not in path.parts and "scripts" not in path.parts and ".serena" not in path.parts
    ]
    combined_source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    _require("uvmapping.auto_unwrap" in combined_source, "자동 언랩 연산자 id를 소스에서 찾을 수 없습니다.")
    _require("def register(" in combined_source, "register 함수를 소스에서 찾을 수 없습니다.")
    _require("def unregister(" in combined_source, "unregister 함수를 소스에서 찾을 수 없습니다.")


def _check_macos_launcher() -> None:
    script = _read("scripts/dev_run.sh")
    required_tokens = (
        "set -euo pipefail",
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "BLENDER_USER_RESOURCES",
        PROFILE_NAME,
        "extensions/user_default",
        ADDON_ID,
        "--python-exit-code 1",
        '"$@"',
        ".tmp.$$",
        "mv -f -h",
    )
    for token in required_tokens:
        _require(token in script, f"macOS 실행기에 필수 안전 요소가 없습니다: {token}")
    _require("--factory-startup" not in script, "개발 실행기에 --factory-startup을 기본 적용하면 안 됩니다.")
    _require("rm -rf" not in script, "macOS 실행기에 광범위한 삭제 명령이 있습니다.")


def _check_windows_launchers() -> None:
    powershell_bytes = (ROOT / "scripts/dev_run.ps1").read_bytes()
    _require(powershell_bytes.startswith(b"\xef\xbb\xbf"), "dev_run.ps1은 Windows PowerShell 5.1용 UTF-8 BOM이어야 합니다.")
    powershell = powershell_bytes.decode("utf-8-sig")
    required_tokens = (
        "portable",
        "extensions\\user_default",
        ADDON_ID,
        PROFILE_NAME,
        "Junction",
        "ReparsePoint",
        "--python-exit-code",
        "$LASTEXITCODE",
        '"gui", "link", "background", "expression", "file"',
        "$BlenderArguments.Add",
    )
    for token in required_tokens:
        _require(token in powershell, f"Windows 실행기에 필수 안전 요소가 없습니다: {token}")
    _require("Remove-Item -Recurse" not in powershell, "Windows 실행기가 디렉터리를 재귀 삭제하면 안 됩니다.")
    _require("--factory-startup" not in powershell, "Windows 개발 실행기에 --factory-startup을 적용하면 안 됩니다.")

    batch = _read("scripts/dev_run.bat")
    _require("%*" in batch, "배치 실행기가 모든 인자를 전달하지 않습니다.")
    _require("%ERRORLEVEL%" in batch, "배치 실행기가 PowerShell 종료 코드를 읽지 않습니다.")
    _require("exit /b" in batch.lower(), "배치 실행기가 종료 코드를 반환하지 않습니다.")
    _require(all(ord(character) < 128 for character in batch), "배치 파일에는 코드페이지 의존 비 ASCII 문자가 없어야 합니다.")


def _check_bootstrap_and_docs() -> None:
    bootstrap = _read("scripts/dev_bootstrap.py")
    for token in (
        "bpy.utils.resource_path",
        "extensions",
        "user_default",
        ADDON_ID,
        "bpy.ops.preferences.addon_enable",
        "bpy.ops.wm.save_userpref",
        "bl_ext.user_default",
    ):
        _require(token in bootstrap, f"부트스트랩에 필수 검사가 없습니다: {token}")

    readme = _read("README.md")
    _require("격리 개발 프로필" in readme, "README에 개발 프로필 설명이 없습니다.")
    _require("사용자용 원격 설치와의 구분" in readme, "README가 로컬 개발과 원격 설치를 구분하지 않습니다.")
    _require("현재 공개 Extension 저장소" in readme, "README가 원격 배포 상태를 명시하지 않습니다.")


def main() -> None:
    _check_required_files()
    version = _check_manifest()
    _check_operator_source()
    _check_macos_launcher()
    _check_windows_launchers()
    _check_bootstrap_and_docs()
    print(f"프로젝트 정적 검사 통과: id={ADDON_ID}, version={version}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"프로젝트 정적 검사 실패: {error}", file=sys.stderr)
        raise
