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
    ".github/workflows/ci.yml",
    ".github/workflows/extension-pages.yml",
    ".github/workflows/release.yml",
    "blender_manifest.toml",
    "distribution/releases.lock.json",
    "__init__.py",
    "scripts/check_extension_repository.py",
    "scripts/dev_run.sh",
    "scripts/dev_run.ps1",
    "scripts/dev_run.bat",
    "scripts/dev_bootstrap.py",
    "uvmapping/clipboard_image.py",
    "uvmapping/contracts.py",
    "uvmapping/quality.py",
    "uvmapping/texture_job.py",
    "uvmapping/native_input.py",
    "uvmapping/openrouter_provider.py",
    "uvmapping/texture_bake.py",
    "uvmapping/texture_pipeline.py",
    "uvmapping/texture_operators.py",
    "uvmapping/texture_worker.py",
    "tests/blender_texture.py",
    "tests/blender_texture_live.py",
    "tests/test_quality_pure.py",
    "tests/test_clipboard_image_pure.py",
    "tests/test_extension_repository_pure.py",
    "tests/test_native_input_pure.py",
    "tests/test_openrouter_provider_pure.py",
    "tests/test_texture_bake_pure.py",
    "tests/test_texture_job_pure.py",
    "tests/test_texture_pipeline_pure.py",
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
    permissions = manifest.get("permissions", {})
    _require(bool(permissions.get("network")), "AI 호출용 network 권한 설명이 없습니다.")
    _require(bool(permissions.get("files")), "참조와 결과용 files 권한 설명이 없습니다.")
    return version


def _check_operator_source() -> None:
    source_files = [
        path
        for path in ROOT.rglob("*.py")
        if "tests" not in path.parts and "scripts" not in path.parts and ".serena" not in path.parts
    ]
    combined_source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    _require(
        "uvmapping.generate_turnaround" in combined_source,
        "3면도 생성 연산자 id를 소스에서 찾을 수 없습니다.",
    )
    _require(
        "uvmapping.bake_diffuse" in combined_source,
        "Diffuse 베이크 연산자 id를 소스에서 찾을 수 없습니다.",
    )
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


def _check_bootstrap_and_user_docs() -> None:
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
    required_tokens = (
        "## 요구 사항",
        "## 설치",
        "## 사용법",
        "## 라이선스",
        "https://zzamjak-cloud.github.io/UVmapping-Blender/index.json",
        "Check for Updates on Startup",
        "무인으로 자동 설치되지 않습니다",
        "3D Viewport > Sidebar(N) > UV Mapping",
        "텍스처 크기",
        "UV 패딩",
        "Pack Islands",
        "0-1 안에 배치",
        "서로 겹치지 않아야",
        "AI 손맵 텍스처",
        "단일 다면도 생성",
        "OpenRouter API 키 하나",
        "OPENROUTER_API_KEY",
        "openrouter.ai/keys",
        "Blender 개인 환경설정",
        "클립보드",
        "한글 프롬프트 입력",
        "GPL-3.0-or-later",
    )
    for token in required_tokens:
        _require(token in readme, f"README 사용자 안내에 필수 내용이 없습니다: {token}")

    removed_development_tokens = (
        "격리 개발 프로필",
        "dev_run.sh",
        "dev_run.ps1",
        "tests/check_project.py",
        "TextureJob",
        "선택 객체 모두 처리",
        "자동 UV 언랩",
    )
    for token in removed_development_tokens:
        _require(token not in readme, f"README에 사용자와 무관한 개발 문서가 남아 있습니다: {token}")


def _check_extension_pages_workflow() -> None:
    workflow = _read(".github/workflows/extension-pages.yml")
    required_tokens = (
        "release:",
        "types: [published]",
        "workflow_dispatch:",
        "relay:",
        "github.event_name == 'release'",
        "actions: write",
        "gh workflow run extension-pages.yml",
        "contents: read",
        "pages: write",
        "id-token: write",
        "concurrency:",
        "group: pages",
        "cancel-in-progress: false",
        "build:",
        "deploy:",
        "needs: build",
        "github.event_name == 'workflow_dispatch' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
        "ref: ${{ github.sha }}",
        "https://download.blender.org/release/Blender4.5",
        "blender-4.5.13-linux-x64.tar.xz",
        "blender-4.5.13.sha256",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0",
        "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d # v6.0.0",
        "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0",
        "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128 # v5.0.0",
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
        "publish_latest",
        "verified-releases",
        "published_count",
        "Pages에는 SemVer 최신 패키지 하나만 게시해야 합니다.",
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
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d",
        "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9",
        "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",
    }
    _require(used_actions == expected_actions, "Pages 워크플로우의 Action 집합 또는 고정 SHA가 다릅니다.")
    relay_block, remaining_jobs = workflow.split("\n  build:\n", maxsplit=1)
    build_block, deploy_block = remaining_jobs.split("\n  deploy:\n", maxsplit=1)
    _require("permissions:\n      actions: write" in relay_block, "Release 릴레이 작업에는 actions 쓰기 권한만 있어야 합니다.")
    _require("contents: write" not in relay_block, "Release 릴레이가 저장소 내용을 수정하면 안 됩니다.")
    _require("environment:" not in relay_block, "Release 태그 릴레이가 보호된 Pages 환경을 사용하면 안 됩니다.")
    _require(workflow.count("actions: write") == 1, "actions 쓰기 권한은 Release 릴레이 하나에만 있어야 합니다.")
    _require(
        workflow.count(
            "if: github.event_name == 'workflow_dispatch' && "
            "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
        )
        == 2,
        "Pages build와 deploy는 기본 브랜치 workflow_dispatch에서만 실행해야 합니다.",
    )
    _require("gh workflow run" not in build_block + deploy_block, "Pages 실행 릴레이는 release 작업에만 있어야 합니다.")
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


def _check_ci_workflow() -> None:
    workflow = _read(".github/workflows/ci.yml")
    required_tokens = (
        "push:",
        "branches: [main]",
        "pull_request:",
        "workflow_dispatch:",
        "permissions: {}",
        "contents: read",
        "concurrency:",
        "cancel-in-progress: true",
        "Blender headless EGL 런타임 설치",
        "sudo apt-get update",
        "sudo apt-get install --yes --no-install-recommends libegl1",
        "python3 tests/check_project.py",
        "tests/test_*_pure.py",
        "https://download.blender.org/release/Blender4.5",
        "blender-4.5.13-linux-x64.tar.xz",
        "blender-4.5.13.sha256",
        'PYTHONUNBUFFERED: "1"',
        "${#checksum_matches[@]} != 1",
        "sha256sum -c -",
        "scripts/dev_bootstrap.py",
                    "tests/blender_texture.py",
        'echo "::group::Blender 통합 회귀: ${test_file}"',
        'Blender 통합 테스트 실패: ${test_file}',
        "extension validate .",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0",
    )
    for token in required_tokens:
        _require(token in workflow, f"CI 워크플로우에 필수 요소가 없습니다: {token}")

    _require("pull_request_target:" not in workflow, "CI는 pull_request_target에서 실행하면 안 됩니다.")
    _require("contents: write" not in workflow, "CI에는 저장소 쓰기 권한이 필요하지 않습니다.")
    _require("apt-get upgrade" not in workflow, "CI에서 시스템 패키지 전체 업그레이드를 실행하면 안 됩니다.")
    _require(
        not re.search(r"^\s+uses:\s+[^#\s]+@v\d+", workflow, flags=re.MULTILINE),
        "CI Action은 변경 가능한 메이저 태그가 아닌 전체 커밋 SHA로 고정해야 합니다.",
    )
    used_actions = set(re.findall(r"^\s+uses:\s+([^\s#]+)", workflow, flags=re.MULTILINE))
    expected_actions = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
    }
    _require(used_actions == expected_actions, "CI Action 집합 또는 고정 SHA가 다릅니다.")
    run_directives = re.findall(r"^\s+run:\s*(.*)$", workflow, flags=re.MULTILINE)
    _require(run_directives and all(value == "|" for value in run_directives), "CI 실행 단계는 모두 블록 셸이어야 합니다.")
    _require(
        len(run_directives) == workflow.count("set -euo pipefail"),
        "모든 CI 실행 단계는 set -euo pipefail로 시작해야 합니다.",
    )


def _check_release_workflow() -> None:
    workflow = _read(".github/workflows/release.yml")
    required_tokens = (
        "push:",
        "tags:",
        '- "v*"',
        "permissions: {}",
        "contents: read",
        "contents: write",
        "cancel-in-progress: false",
        "Blender headless EGL 런타임 설치",
        "sudo apt-get update",
        "sudo apt-get install --yes --no-install-recommends libegl1",
        "GITHUB_REF_TYPE",
        "GITHUB_REF_NAME",
        '"v${manifest_version}"',
        "python3 tests/check_project.py",
        "tests/test_*_pure.py",
        "https://download.blender.org/release/Blender4.5",
        "blender-4.5.13-linux-x64.tar.xz",
        "blender-4.5.13.sha256",
        'PYTHONUNBUFFERED: "1"',
        "${#checksum_matches[@]} != 1",
        "sha256sum -c -",
        "scripts/dev_bootstrap.py",
                    "tests/blender_texture.py",
        'echo "::group::Blender 통합 회귀: ${test_file}"',
        'Blender 통합 테스트 실패: ${test_file}',
        "extension validate .",
        "extension build",
        "--output-filepath",
        'ASSET_NAME=uvmapping_blender-v${manifest_version}.zip',
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1",
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1",
        "needs: verify",
        "gh release create",
        "--verify-tag",
        "--draft",
        "gh release upload",
        "--clobber",
        "isDraft",
        "distribution/releases.lock.json",
        "GITHUB_STEP_SUMMARY",
    )
    for token in required_tokens:
        _require(token in workflow, f"Release 워크플로우에 필수 요소가 없습니다: {token}")

    _require("pull_request_target:" not in workflow, "Release는 pull_request_target에서 실행하면 안 됩니다.")
    _require("types: [published]" not in workflow, "태그 워크플로우가 공개 Release 이벤트를 사용하면 안 됩니다.")
    _require("apt-get upgrade" not in workflow, "Release에서 시스템 패키지 전체 업그레이드를 실행하면 안 됩니다.")
    _require("gh release edit" not in workflow, "태그 워크플로우가 Release를 공개하면 안 됩니다.")
    _require("--draft=false" not in workflow, "태그 워크플로우가 초안을 공개하면 안 됩니다.")
    _require(
        not re.search(r"^\s+uses:\s+[^#\s]+@v\d+", workflow, flags=re.MULTILINE),
        "Release Action은 변경 가능한 메이저 태그가 아닌 전체 커밋 SHA로 고정해야 합니다.",
    )
    used_actions = set(re.findall(r"^\s+uses:\s+([^\s#]+)", workflow, flags=re.MULTILINE))
    expected_actions = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    }
    _require(used_actions == expected_actions, "Release Action 집합 또는 고정 SHA가 다릅니다.")
    verify_block, draft_block = workflow.split("\n  draft:\n", maxsplit=1)
    _require("contents: write" not in verify_block, "Release 검증 작업에는 쓰기 권한이 필요하지 않습니다.")
    _require("permissions:\n      contents: write" in draft_block, "초안 생성 작업에만 contents 쓰기 권한이 있어야 합니다.")
    run_directives = re.findall(r"^\s+run:\s*(.*)$", workflow, flags=re.MULTILINE)
    _require(run_directives and all(value == "|" for value in run_directives), "Release 실행 단계는 모두 블록 셸이어야 합니다.")
    _require(
        len(run_directives) == workflow.count("set -euo pipefail"),
        "모든 Release 실행 단계는 set -euo pipefail로 시작해야 합니다.",
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
        "_version_tuple",
        "max(locked_releases",
        'asset.get("digest")',
        "package.testzip()",
        '".." not in member_path.parts',
        'archive_url == f"./{expected_name}"',
        "archive_size",
        "archive_hash",
        "blender_version_min",
        "package_sha256 == locked",
        '"1" if str(locked["version"]) == latest_version else "0"',
        "len(entry_ids) == len(set(entry_ids))",
        "SemVer 최신 ZIP 하나만",
    )
    for token in required_tokens:
        _require(token in script, f"Extension 저장소 검사기에 필수 검사가 없습니다: {token}")


def _check_release_lock() -> None:
    lock = json.loads(_read("distribution/releases.lock.json"))
    _require(lock.get("schema_version") == "1.0.0", "Release 잠금 스키마는 1.0.0이어야 합니다.")
    releases = lock.get("releases")
    _require(
        isinstance(releases, list) and len(releases) == 6,
        "현재 Release 잠금에는 v1.1.0, v1.2.0, v2.0.0, v2.1.0, v2.2.0, v2.3.0이 있어야 합니다.",
    )
    expected = [
        {
            "tag": "v1.1.0",
            "version": "1.1.0",
            "asset_name": "uvmapping_blender-v1.1.0.zip",
            "url": "https://github.com/zzamjak-cloud/UVmapping-Blender/releases/download/v1.1.0/uvmapping_blender-v1.1.0.zip",
            "size": 44608,
            "sha256": "0fa13ce9c3b71170636a286fa355c7ba60c45df8796efe58300fdf526a900de5",
            "target_commit": "c74588ca9b3abe8ee9f1d644189f9491d8f6b065",
        },
        {
            "tag": "v1.2.0",
            "version": "1.2.0",
            "asset_name": "uvmapping_blender-v1.2.0.zip",
            "url": "https://github.com/zzamjak-cloud/UVmapping-Blender/releases/download/v1.2.0/uvmapping_blender-v1.2.0.zip",
            "size": 96744,
            "sha256": "7c73656fd0efbfc66ff6959fb8954168f9336f2796aa4925e57550deaf84cebd",
            "target_commit": "b675fc85fb2e9a169a9e9b375501c0d0f5d73828",
        },
        {
            "tag": "v2.0.0",
            "version": "2.0.0",
            "asset_name": "uvmapping_blender-v2.0.0.zip",
            "url": "https://github.com/zzamjak-cloud/UVmapping-Blender/releases/download/v2.0.0/uvmapping_blender-v2.0.0.zip",
            "size": 75901,
            "sha256": "a41f306722a91e8d3137be585e44ee41e2f6883e2b99510e762241db1a3f0695",
            "target_commit": "8a4e02b13e4f662334b66f11f6a2e510602bca6d",
        },
        {
            "tag": "v2.1.0",
            "version": "2.1.0",
            "asset_name": "uvmapping_blender-v2.1.0.zip",
            "url": "https://github.com/zzamjak-cloud/UVmapping-Blender/releases/download/v2.1.0/uvmapping_blender-v2.1.0.zip",
            "size": 74730,
            "sha256": "2c6e2afe677e0165434e71735c367bf7180b27329806ce66ede044abe762ac39",
            "target_commit": "1426b257ab863b0f096f93682c485e8189ea1655",
        },
        {
            "tag": "v2.2.0",
            "version": "2.2.0",
            "asset_name": "uvmapping_blender-v2.2.0.zip",
            "url": "https://github.com/zzamjak-cloud/UVmapping-Blender/releases/download/v2.2.0/uvmapping_blender-v2.2.0.zip",
            "size": 84976,
            "sha256": "42934925c5abccd497e5da1939026ca5ebf0adaaeaae3362b2456824080e1786",
            "target_commit": "a6841212175d05e509b9f5bacd79dcc6fb41dc76",
        },
        {
            "tag": "v2.3.0",
            "version": "2.3.0",
            "asset_name": "uvmapping_blender-v2.3.0.zip",
            "url": "https://github.com/zzamjak-cloud/UVmapping-Blender/releases/download/v2.3.0/uvmapping_blender-v2.3.0.zip",
            "size": 125636,
            "sha256": "977abf9611520946ed1386c15da3b6050f438a2190754889c34db8bd4c8accad",
            "target_commit": "b348b0a39bb9fee3d52377773eb2ee670b80556a",
        },
    ]
    _require(releases == expected, "Release 잠금 메타데이터가 승인된 값과 다릅니다.")


def main() -> None:
    _check_required_files()
    version = _check_manifest()
    _check_operator_source()
    _check_macos_launcher()
    _check_windows_launchers()
    _check_bootstrap_and_user_docs()
    _check_ci_workflow()
    _check_release_workflow()
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
