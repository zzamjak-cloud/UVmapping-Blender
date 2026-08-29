"""격리된 Blender 개발 프로필에서 Extension을 활성화합니다."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tomllib

import bpy


def _required_environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"필수 환경 변수가 없습니다: {name}")
    return Path(value).expanduser().resolve()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _normalize_version(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)):
        return ".".join(str(part) for part in value)
    return None


def main() -> None:
    repository_root = _required_environment_path("UVMAPPING_REPOSITORY_ROOT")
    profile_root = _required_environment_path("UVMAPPING_PROFILE_ROOT")
    addon_id = os.environ.get("UVMAPPING_ADDON_ID", "")
    if addon_id != "uvmapping_blender":
        raise RuntimeError(f"예상하지 못한 Extension id입니다: {addon_id!r}")

    user_resource = Path(bpy.utils.resource_path("USER")).resolve()
    if not _same_path(user_resource, profile_root):
        raise RuntimeError(
            "Blender 사용자 리소스가 격리되지 않았습니다: "
            f"expected={profile_root}, actual={user_resource}"
        )

    addon_link = profile_root / "extensions" / "user_default" / addon_id
    if not addon_link.exists() or not _same_path(addon_link, repository_root):
        raise RuntimeError(f"개발 Extension 링크가 저장소를 가리키지 않습니다: {addon_link}")

    manifest_path = repository_root / "blender_manifest.toml"
    with manifest_path.open("rb") as manifest_file:
        manifest = tomllib.load(manifest_file)
    if manifest.get("id") != addon_id:
        raise RuntimeError("매니페스트 id와 개발 Extension id가 일치하지 않습니다.")
    manifest_version = str(manifest.get("version", ""))
    if not manifest_version:
        raise RuntimeError("매니페스트 version이 비어 있습니다.")

    module_name = f"bl_ext.user_default.{addon_id}"
    was_enabled = module_name in bpy.context.preferences.addons
    if not was_enabled:
        result = bpy.ops.preferences.addon_enable(module=module_name)
        if "FINISHED" not in result:
            raise RuntimeError(f"Extension 활성화에 실패했습니다: {result}")
        bpy.ops.wm.save_userpref()

    module = importlib.import_module(module_name)
    source_version = _normalize_version(getattr(module, "__version__", None))
    if source_version is None:
        bl_info = getattr(module, "bl_info", {})
        source_version = _normalize_version(bl_info.get("version")) if isinstance(bl_info, dict) else None
    if source_version is not None and source_version != manifest_version:
        raise RuntimeError(
            f"소스 버전({source_version})과 매니페스트 버전({manifest_version})이 다릅니다."
        )

    print(f"[UVmapping Blender] 개발 프로필: {profile_root}")
    print(f"[UVmapping Blender] 활성 모듈: {module_name} ({manifest_version})")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"[UVmapping Blender] 부트스트랩 실패: {error}", file=sys.stderr)
        raise

