"""Blender 없이 Extension 저장소의 최신 버전 선택을 검사합니다."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_extension_repository.py"
SPEC = importlib.util.spec_from_file_location("check_extension_repository", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
repository_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repository_check)


def _write_package(directory: Path, version: str) -> Path:
    package_path = directory / f"uvmapping_blender-v{version}.zip"
    manifest = (
        'schema_version = "1.0.0"\n'
        'id = "uvmapping_blender"\n'
        f'version = "{version}"\n'
        'name = "UV Mapping Blender"\n'
        'tagline = "테스트 패키지"\n'
        'maintainer = "zzamjak-cloud"\n'
        'type = "add-on"\n'
        'blender_version_min = "4.5.0"\n'
        'license = ["SPDX:GPL-3.0-or-later"]\n'
    )
    with zipfile.ZipFile(package_path, "w") as package:
        package.writestr("blender_manifest.toml", manifest)
        package.writestr("__init__.py", "")
    return package_path


def _locked_release(package_path: Path, version: str) -> dict[str, object]:
    asset_name = package_path.name
    return {
        "tag": f"v{version}",
        "version": version,
        "asset_name": asset_name,
        "url": (
            "https://github.com/zzamjak-cloud/UVmapping-Blender/"
            f"releases/download/v{version}/{asset_name}"
        ),
        "size": package_path.stat().st_size,
        "sha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
        "target_commit": ("1" if version == "1.9.0" else "2") * 40,
    }


def _api_release(release: dict[str, object], asset_id: int) -> dict[str, object]:
    return {
        "draft": False,
        "prerelease": False,
        "tag_name": release["tag"],
        "assets": [
            {
                "id": asset_id,
                "name": release["asset_name"],
                "size": release["size"],
                "digest": f"sha256:{release['sha256']}",
                "browser_download_url": release["url"],
            }
        ],
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _expect_assertion(function, expected_message: str) -> None:
    try:
        function()
    except AssertionError as error:
        assert expected_message in str(error)
    else:
        raise AssertionError("예상한 저장소 검증 실패가 발생하지 않았습니다.")


def test_release_plan_validates_all_and_marks_integer_semver_latest() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        old_package = _write_package(root, "1.9.0")
        latest_package = _write_package(root, "1.10.0")
        old_release = _locked_release(old_package, "1.9.0")
        latest_release = _locked_release(latest_package, "1.10.0")
        lock_path = root / "releases.lock.json"
        api_path = root / "releases.json"
        _write_json(
            lock_path,
            {"schema_version": "1.0.0", "releases": [latest_release, old_release]},
        )
        _write_json(
            api_path,
            [_api_release(latest_release, 110), _api_release(old_release, 109)],
        )

        plan = repository_check.build_release_plan(lock_path, api_path)

        assert [record[2] for record in plan] == ["1.9.0", "1.10.0"]
        assert [record[-1] for record in plan] == ["0", "1"]


def test_repository_accepts_only_latest_zip_and_rejects_duplicate_id() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        packages = root / "packages"
        repository = root / "public"
        packages.mkdir()
        repository.mkdir()
        old_package = _write_package(packages, "1.9.0")
        latest_package = _write_package(packages, "1.10.0")
        old_release = _locked_release(old_package, "1.9.0")
        latest_release = _locked_release(latest_package, "1.10.0")
        lock_path = root / "releases.lock.json"
        _write_json(
            lock_path,
            {"schema_version": "1.0.0", "releases": [old_release, latest_release]},
        )
        published_package = repository / latest_package.name
        shutil.copyfile(latest_package, published_package)
        package_hash = hashlib.sha256(published_package.read_bytes()).hexdigest()
        entry = {
            "id": "uvmapping_blender",
            "version": "1.10.0",
            "archive_url": f"./{published_package.name}",
            "archive_size": published_package.stat().st_size,
            "archive_hash": f"sha256:{package_hash}",
            "blender_version_min": "4.5.0",
        }
        _write_json(repository / "index.json", {"data": [entry]})
        (repository / "index.html").write_text("latest", encoding="utf-8")

        assert repository_check.check_repository(repository, lock_path) == (
            1,
            ("1.10.0",),
        )

        old_public = repository / old_package.name
        shutil.copyfile(old_package, old_public)
        _expect_assertion(
            lambda: repository_check.check_repository(repository, lock_path),
            "SemVer 최신 ZIP 하나만",
        )
        old_public.unlink()

        _write_json(repository / "index.json", {"data": [entry, dict(entry)]})
        _expect_assertion(
            lambda: repository_check.check_repository(repository, lock_path),
            "중복된 Extension id",
        )


if __name__ == "__main__":
    tests = sorted(
        (
            (name, value)
            for name, value in globals().items()
            if name.startswith("test_") and callable(value)
        ),
        key=lambda item: item[0],
    )
    for _, test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"Extension 저장소 순수 테스트 {len(tests)}/{len(tests)} 통과")
