"""Blender를 실행하지 않고 프로젝트 구조와 개발 실행기를 검사합니다."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
ADDON_ID = "uvmapping_blender"
PROFILE_NAME = "UVmappingBlenderDev"
REQUIRED_FILES = (
    ".github/workflows/extension-pages.yml",
    "blender_manifest.toml",
    "distribution/releases.lock.json",
    "__init__.py",
    "scripts/check_extension_repository.py",
    "scripts/dev_run.sh",
    "scripts/dev_run.ps1",
    "scripts/dev_run.bat",
    "scripts/dev_bootstrap.py",
    "tests/blender_smoke.py",
    "tests/blender_quality.py",
    "tests/blender_v1.py",
    "tests/test_analysis_pure.py",
    "tests/test_quality_pure.py",
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
    _require(
        "https://zzamjak-cloud.github.io/UVmapping-Blender/index.json" in readme,
        "README가 공개 Extension 저장소 URL을 명시하지 않습니다.",
    )


def _check_extension_pages_workflow() -> None:
    workflow = _read(".github/workflows/extension-pages.yml")
    required_tokens = (
        "release:",
        "types: [published]",
        "workflow_dispatch:",
        "contents: read",
        "pages: write",
        "id-token: write",
        "concurrency:",
        "group: pages",
        "cancel-in-progress: false",
        "build:",
        "deploy:",
        "needs: build",
        "github.event_name != 'pull_request'",
        "https://download.blender.org/release/Blender4.5",
        "blender-4.5.13-linux-x64.tar.xz",
        "blender-4.5.13.sha256",
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
        "ref: ${{ github.event.repository.default_branch }}",
        "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830 # v4.3.0",
        "actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b # v5.0.0",
        "actions/upload-pages-artifact@56afc609e74202658d3ffba0e8f6dda462b719fa # v3.0.1",
        "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e # v4.0.5",
        "${#checksum_matches[@]} != 1",
        "sha256sum -c -",
        "gh api --paginate --slurp",
        "distribution/releases.lock.json",
        "git rev-parse",
        "extension validate",
        "extension server-generate",
        "--repo-dir=public",
        "--html",
        "scripts/check_extension_repository.py releases",
        "scripts/check_extension_repository.py repository",
    )
    for token in required_tokens:
        _require(token in workflow, f"Pages 워크플로우에 필수 요소가 없습니다: {token}")

    _require("pull_request:" not in workflow, "Pages 배포를 pull_request 이벤트에서 실행하면 안 됩니다.")
    _require(
        "pull_request_target:" not in workflow,
        "Pages 배포를 pull_request_target 이벤트에서 실행하면 안 됩니다.",
    )
    _require(
        not re.search(r"^\s+uses:\s+[^#\s]+@v\d+", workflow, flags=re.MULTILINE),
        "Pages 워크플로우 Action은 변경 가능한 메이저 태그가 아닌 전체 커밋 SHA로 고정해야 합니다.",
    )
    used_actions = set(re.findall(r"^\s+uses:\s+([^\s#]+)", workflow, flags=re.MULTILINE))
    expected_actions = {
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830",
        "actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b",
        "actions/upload-pages-artifact@56afc609e74202658d3ffba0e8f6dda462b719fa",
        "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
    }
    _require(used_actions == expected_actions, "Pages 워크플로우의 Action 집합 또는 고정 SHA가 다릅니다.")
    build_block, deploy_block = workflow.split("\n  deploy:\n", maxsplit=1)
    _require("permissions:\n      contents: read" in build_block, "build 작업은 contents: read만 사용해야 합니다.")
    _require("pages: write" not in build_block, "build 작업에 Pages 쓰기 권한이 있으면 안 됩니다.")
    _require("permissions:\n      pages: write\n      id-token: write" in deploy_block, "deploy 작업의 Pages 권한이 올바르지 않습니다.")
    _require("contents: read" not in deploy_block, "deploy 작업에 불필요한 contents 권한이 있으면 안 됩니다.")
    run_directives = re.findall(r"^\s+run:\s*(.*)$", workflow, flags=re.MULTILINE)
    _require(
        all(directive == "|" for directive in run_directives),
        "Pages 워크플로우의 모든 실행 단계는 검사 가능한 블록 셸이어야 합니다.",
    )
    run_block_count = len(run_directives)
    strict_shell_count = workflow.count("set -euo pipefail")
    _require(run_block_count > 0, "Pages 워크플로우에 실행 단계가 없습니다.")
    _require(
        run_block_count == strict_shell_count,
        "모든 Pages 워크플로우 실행 단계는 set -euo pipefail로 시작해야 합니다.",
    )


def _check_extension_repository_script() -> None:
    script = _read("scripts/check_extension_repository.py")
    compile(script, "scripts/check_extension_repository.py", "exec")
    required_tokens = (
        "index.json",
        "index.html",
        "blender_manifest.toml",
        "tomllib",
        "zipfile",
        "set(api_by_tag) == set(locked_by_tag)",
        'asset.get("digest")',
        "package.testzip()",
        '".." not in member_path.parts',
        'archive_url == f"./{expected_name}"',
        "archive_size",
        "archive_hash",
        "blender_version_min",
        "package_sha256 == locked",
    )
    for token in required_tokens:
        _require(token in script, f"Extension 저장소 검사기에 필수 검사가 없습니다: {token}")


def _check_release_lock() -> None:
    lock = json.loads(_read("distribution/releases.lock.json"))
    _require(lock.get("schema_version") == "1.0.0", "Release 잠금 스키마는 1.0.0이어야 합니다.")
    releases = lock.get("releases")
    _require(isinstance(releases, list) and len(releases) == 1, "현재 Release 잠금에는 v1.1.0 하나가 있어야 합니다.")
    expected = {
        "tag": "v1.1.0",
        "version": "1.1.0",
        "asset_name": "uvmapping_blender-v1.1.0.zip",
        "url": "https://github.com/zzamjak-cloud/UVmapping-Blender/releases/download/v1.1.0/uvmapping_blender-v1.1.0.zip",
        "size": 44608,
        "sha256": "0fa13ce9c3b71170636a286fa355c7ba60c45df8796efe58300fdf526a900de5",
        "target_commit": "c74588ca9b3abe8ee9f1d644189f9491d8f6b065",
    }
    _require(releases[0] == expected, "v1.1.0 Release 잠금 메타데이터가 승인된 값과 다릅니다.")


def main() -> None:
    _check_required_files()
    version = _check_manifest()
    _check_operator_source()
    _check_macos_launcher()
    _check_windows_launchers()
    _check_bootstrap_and_docs()
    _check_extension_pages_workflow()
    _check_extension_repository_script()
    _check_release_lock()
    print(f"프로젝트 정적 검사 통과: id={ADDON_ID}, version={version}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"프로젝트 정적 검사 실패: {error}", file=sys.stderr)
        raise
