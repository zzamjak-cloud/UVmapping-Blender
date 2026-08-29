"""Release 잠금 정보와 생성된 Blender Extension 저장소를 검증합니다."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tomllib
from typing import Any
import zipfile


ADDON_ID = "uvmapping_blender"
REPOSITORY_URL = "https://github.com/zzamjak-cloud/UVmapping-Blender"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
PACKAGE_PATTERN = re.compile(
    rf"^{re.escape(ADDON_ID)}-v(?P<version>\d+\.\d+\.\d+)\.zip$"
)
LOCK_KEYS = {
    "tag",
    "version",
    "asset_name",
    "url",
    "size",
    "sha256",
    "target_commit",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _load_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionError(f"{description}을 읽을 수 없습니다: {error}") from error


def _load_lock(lock_path: Path) -> tuple[dict[str, Any], ...]:
    lock_data = _load_json(lock_path, "Release 잠금 파일")
    _require(isinstance(lock_data, dict), "Release 잠금 파일의 최상위 값은 객체여야 합니다.")
    _require(lock_data.get("schema_version") == "1.0.0", "Release 잠금 스키마는 1.0.0이어야 합니다.")
    releases = lock_data.get("releases")
    _require(isinstance(releases, list) and releases, "Release 잠금 목록은 비어 있지 않은 배열이어야 합니다.")

    normalized: list[dict[str, Any]] = []
    for index, release in enumerate(releases):
        _require(isinstance(release, dict), f"잠금 Release #{index + 1}은 객체여야 합니다.")
        _require(set(release) == LOCK_KEYS, f"잠금 Release #{index + 1}의 필드가 올바르지 않습니다.")
        version = release.get("version")
        tag = release.get("tag")
        asset_name = release.get("asset_name")
        sha256 = release.get("sha256")
        target_commit = release.get("target_commit")
        size = release.get("size")
        url = release.get("url")
        _require(isinstance(version, str) and VERSION_PATTERN.fullmatch(version), f"유효하지 않은 잠금 버전: {version!r}")
        _require(tag == f"v{version}", f"잠금 태그와 버전이 일치하지 않습니다: {tag!r}")
        _require(asset_name == f"{ADDON_ID}-v{version}.zip", f"잠금 자산 이름이 올바르지 않습니다: {asset_name!r}")
        _require(isinstance(size, int) and size > 0, f"잠금 자산 크기가 올바르지 않습니다: {size!r}")
        _require(isinstance(sha256, str) and SHA256_PATTERN.fullmatch(sha256), f"잠금 SHA-256이 올바르지 않습니다: {sha256!r}")
        _require(
            isinstance(target_commit, str) and COMMIT_PATTERN.fullmatch(target_commit),
            f"잠금 대상 커밋이 올바르지 않습니다: {target_commit!r}",
        )
        expected_url = f"{REPOSITORY_URL}/releases/download/{tag}/{asset_name}"
        _require(url == expected_url, f"잠금 자산 URL이 올바르지 않습니다: {url!r}")
        normalized.append(dict(release))

    for field in ("tag", "version", "asset_name"):
        values = [str(release[field]) for release in normalized]
        _require(len(values) == len(set(values)), f"잠금 파일에 중복된 {field} 값이 있습니다.")
    return tuple(sorted(normalized, key=lambda release: str(release["version"])))


def _flatten_api_pages(api_data: Any) -> tuple[dict[str, Any], ...]:
    _require(isinstance(api_data, list), "GitHub Release API 응답은 배열이어야 합니다.")
    if api_data and all(isinstance(page, list) for page in api_data):
        values = [release for page in api_data for release in page]
    else:
        values = list(api_data)
    _require(all(isinstance(release, dict) for release in values), "GitHub Release API 항목은 객체여야 합니다.")
    return tuple(values)


def build_release_plan(lock_path: Path, api_path: Path) -> tuple[tuple[str, ...], ...]:
    """안정 Release와 잠금 집합을 대조하고 다운로드용 TSV 레코드를 만듭니다."""

    locked_releases = _load_lock(lock_path)
    api_releases = _flatten_api_pages(_load_json(api_path, "GitHub Release API 응답"))
    stable_releases = tuple(
        release
        for release in api_releases
        if release.get("draft") is False and release.get("prerelease") is False
    )
    locked_by_tag = {str(release["tag"]): release for release in locked_releases}
    api_by_tag = {str(release.get("tag_name", "")): release for release in stable_releases}
    _require(len(api_by_tag) == len(stable_releases), "GitHub 안정 Release에 중복된 태그가 있습니다.")
    _require(
        set(api_by_tag) == set(locked_by_tag),
        "GitHub 안정 Release 태그 집합과 releases.lock.json이 정확히 일치하지 않습니다.",
    )

    records: list[tuple[str, ...]] = []
    for tag, locked in sorted(locked_by_tag.items()):
        release = api_by_tag[tag]
        assets = release.get("assets")
        _require(isinstance(assets, list), f"{tag}: GitHub 자산 목록이 없습니다.")
        matching_assets = [
            asset
            for asset in assets
            if isinstance(asset, dict) and asset.get("name") == locked["asset_name"]
        ]
        _require(len(matching_assets) == 1, f"{tag}: 잠긴 Extension ZIP 자산이 정확히 하나여야 합니다.")
        asset = matching_assets[0]
        _require(asset.get("size") == locked["size"], f"{tag}: GitHub 자산 크기가 잠금 값과 다릅니다.")
        _require(
            asset.get("digest") == f"sha256:{locked['sha256']}",
            f"{tag}: GitHub 자산 digest가 잠금 값과 다릅니다.",
        )
        _require(
            asset.get("browser_download_url") == locked["url"],
            f"{tag}: GitHub 자산 URL이 잠금 값과 다릅니다.",
        )
        asset_id = asset.get("id")
        _require(isinstance(asset_id, int) and asset_id > 0, f"{tag}: GitHub 자산 ID가 올바르지 않습니다.")
        records.append(
            (
                str(asset_id),
                tag,
                str(locked["version"]),
                str(locked["asset_name"]),
                str(locked["size"]),
                str(locked["sha256"]),
                str(locked["url"]),
                str(locked["target_commit"]),
            )
        )
    return tuple(records)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_archive_paths(package: zipfile.ZipFile, package_name: str) -> None:
    for member_name in package.namelist():
        member_path = PurePosixPath(member_name)
        _require("\\" not in member_name, f"{package_name}: ZIP 경로에 역슬래시가 있습니다.")
        _require(not member_path.is_absolute(), f"{package_name}: ZIP에 절대 경로가 있습니다.")
        _require(".." not in member_path.parts, f"{package_name}: ZIP에 상위 경로 이동이 있습니다.")


def _read_manifest(package_path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(package_path) as package:
            _require(package.testzip() is None, f"{package_path.name}: 손상된 ZIP 멤버가 있습니다.")
            _validate_archive_paths(package, package_path.name)
            manifest_names = [
                name
                for name in package.namelist()
                if PurePosixPath(name).name == "blender_manifest.toml" and not name.endswith("/")
            ]
            _require(
                len(manifest_names) == 1,
                f"{package_path.name}: blender_manifest.toml이 정확히 하나여야 합니다.",
            )
            manifest_name = manifest_names[0]
            _require(
                PurePosixPath(manifest_name).parent == PurePosixPath("."),
                f"{package_path.name}: blender_manifest.toml은 ZIP 루트에 있어야 합니다.",
            )
            with package.open(manifest_name) as manifest_file:
                return tomllib.loads(manifest_file.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, zipfile.BadZipFile) as error:
        raise AssertionError(f"{package_path.name}: 패키지를 읽을 수 없습니다: {error}") from error


def _validate_archive_url(archive_url: Any, expected_name: str) -> None:
    _require(isinstance(archive_url, str), f"{expected_name}: archive_url이 문자열이 아닙니다.")
    _require(archive_url == f"./{expected_name}", f"{expected_name}: archive_url은 './' 상대 경로여야 합니다.")
    archive_path = PurePosixPath(archive_url)
    _require(".." not in archive_path.parts, f"{expected_name}: archive_url에 상위 경로 이동이 있습니다.")
    _require(not archive_path.is_absolute(), f"{expected_name}: archive_url은 절대 경로일 수 없습니다.")


def check_repository(repository_dir: Path, lock_path: Path) -> tuple[int, tuple[str, ...]]:
    """생성 인덱스와 ZIP의 크기·해시·매니페스트를 잠금 파일과 대조합니다."""

    locked_releases = _load_lock(lock_path)
    _require(repository_dir.is_dir(), f"저장소 디렉터리가 없습니다: {repository_dir}")
    index_path = repository_dir / "index.json"
    html_path = repository_dir / "index.html"
    _require(index_path.is_file(), "Blender가 생성한 index.json이 없습니다.")
    _require(html_path.is_file(), "--html로 생성해야 하는 index.html이 없습니다.")
    index_data = _load_json(index_path, "index.json")
    _require(isinstance(index_data, dict), "index.json의 최상위 값은 객체여야 합니다.")
    index_entries = index_data.get("data")
    _require(isinstance(index_entries, list), "index.json의 data는 배열이어야 합니다.")
    _require(all(isinstance(entry, dict) for entry in index_entries), "index.json의 패키지 항목은 객체여야 합니다.")

    expected_names = {str(release["asset_name"]) for release in locked_releases}
    actual_paths = sorted(repository_dir.glob("*.zip"))
    actual_names = {path.name for path in actual_paths}
    _require(actual_names == expected_names, "저장소 ZIP 집합과 releases.lock.json이 정확히 일치하지 않습니다.")
    _require(len(index_entries) == len(locked_releases), "index.json 패키지 수와 잠금 Release 수가 다릅니다.")
    entries_by_version = {str(entry.get("version", "")): entry for entry in index_entries}
    _require(len(entries_by_version) == len(index_entries), "index.json에 중복된 버전이 있습니다.")

    versions: list[str] = []
    for locked in locked_releases:
        version = str(locked["version"])
        package_path = repository_dir / str(locked["asset_name"])
        _require(package_path.stat().st_size == locked["size"], f"{package_path.name}: 실제 크기가 잠금 값과 다릅니다.")
        package_sha256 = _sha256(package_path)
        _require(package_sha256 == locked["sha256"], f"{package_path.name}: 실제 SHA-256이 잠금 값과 다릅니다.")

        filename_match = PACKAGE_PATTERN.fullmatch(package_path.name)
        _require(bool(filename_match), f"유효하지 않은 패키지 파일명입니다: {package_path.name}")
        manifest = _read_manifest(package_path)
        manifest_id = str(manifest.get("id", ""))
        manifest_version = str(manifest.get("version", ""))
        blender_min = str(manifest.get("blender_version_min", ""))
        _require(manifest_id == ADDON_ID, f"{package_path.name}: 매니페스트 id가 올바르지 않습니다.")
        _require(manifest_version == version, f"{package_path.name}: 매니페스트 버전이 잠금 값과 다릅니다.")
        _require(filename_match.group("version") == version, f"{package_path.name}: 파일명 버전이 잠금 값과 다릅니다.")
        _require(bool(blender_min), f"{package_path.name}: blender_version_min이 없습니다.")

        entry = entries_by_version.get(version)
        _require(entry is not None, f"index.json에 버전이 없습니다: {version}")
        _validate_archive_url(entry.get("archive_url"), package_path.name)
        _require(entry.get("archive_size") == locked["size"], f"{package_path.name}: index archive_size가 다릅니다.")
        _require(
            entry.get("archive_hash") == f"sha256:{package_sha256}",
            f"{package_path.name}: index archive_hash가 다릅니다.",
        )
        _require(entry.get("id") == manifest_id, f"{package_path.name}: index id가 매니페스트와 다릅니다.")
        _require(entry.get("version") == manifest_version, f"{package_path.name}: index version이 매니페스트와 다릅니다.")
        _require(
            entry.get("blender_version_min") == blender_min,
            f"{package_path.name}: index blender_version_min이 매니페스트와 다릅니다.",
        )
        versions.append(version)
    return len(actual_paths), tuple(versions)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    releases_parser = subparsers.add_parser("releases", help="GitHub 안정 Release와 잠금 파일 대조")
    releases_parser.add_argument("lock_path", type=Path)
    releases_parser.add_argument("api_path", type=Path)
    repository_parser = subparsers.add_parser("repository", help="생성된 Extension 저장소 검사")
    repository_parser.add_argument("repository_dir", type=Path)
    repository_parser.add_argument("lock_path", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "releases":
        for record in build_release_plan(args.lock_path.resolve(), args.api_path.resolve()):
            print("\t".join(record))
        return
    package_count, versions = check_repository(
        args.repository_dir.resolve(),
        args.lock_path.resolve(),
    )
    print(
        f"Extension 저장소 검사 통과: 패키지={package_count}, "
        f"버전={', '.join(versions)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Extension 저장소 검사 실패: {error}", file=sys.stderr)
        raise
