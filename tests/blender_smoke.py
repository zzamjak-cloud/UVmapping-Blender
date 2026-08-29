"""격리 개발 프로필에서 자동 UV 연산자의 실제 동작을 검사합니다."""

from __future__ import annotations

import importlib
import math
import os
from pathlib import Path

import bpy


ADDON_ID = "uvmapping_blender"
MODULE_NAME = f"bl_ext.user_default.{ADDON_ID}"
OPERATOR_ID = "uvmapping.auto_unwrap"


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _assert_isolated_profile() -> None:
    repository_root = Path(os.environ["UVMAPPING_REPOSITORY_ROOT"]).resolve()
    profile_root = Path(os.environ["UVMAPPING_PROFILE_ROOT"]).resolve()
    user_resource = Path(bpy.utils.resource_path("USER")).resolve()
    assert _same_path(user_resource, profile_root), (
        f"USER 리소스가 개발 프로필과 다릅니다: {user_resource} != {profile_root}"
    )
    addon_link = profile_root / "extensions" / "user_default" / ADDON_ID
    assert addon_link.exists(), f"개발 Extension 링크가 없습니다: {addon_link}"
    assert _same_path(addon_link, repository_root), (
        f"개발 Extension 링크 대상이 다릅니다: {addon_link.resolve()} != {repository_root}"
    )
    print(f"[smoke] 개발 프로필: {profile_root}")


def _operator_registered() -> bool:
    try:
        bpy.ops.uvmapping.auto_unwrap.get_rna_type()
    except (KeyError, RuntimeError):
        return False
    return True


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _create_meshes() -> list[bpy.types.Object]:
    objects: list[bpy.types.Object] = []

    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(-4.5, 0.0, 0.0))
    objects.append(bpy.context.active_object)

    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=1.0, depth=2.0, location=(-1.5, 0.0, 0.0))
    objects.append(bpy.context.active_object)

    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, location=(1.5, 0.0, 0.0))
    objects.append(bpy.context.active_object)

    bpy.ops.mesh.primitive_torus_add(
        major_segments=24,
        minor_segments=8,
        major_radius=1.0,
        minor_radius=0.3,
        location=(4.5, 0.0, 0.0),
    )
    objects.append(bpy.context.active_object)

    for index, mesh_object in enumerate(objects):
        mesh_object.name = ("Cube", "Cylinder", "Sphere", "Torus")[index]
        while mesh_object.data.uv_layers:
            mesh_object.data.uv_layers.remove(mesh_object.data.uv_layers[0])
        for edge in mesh_object.data.edges:
            edge.use_seam = False
    return objects


def _activate_only(mesh_object: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    mesh_object.select_set(True)
    bpy.context.view_layer.objects.active = mesh_object
    if mesh_object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _assert_unwrapped(mesh_object: bpy.types.Object) -> None:
    uv_layer = mesh_object.data.uv_layers.active
    assert uv_layer is not None, f"{mesh_object.name}: UV 레이어가 생성되지 않았습니다."
    assert len(uv_layer.data) == len(mesh_object.data.loops), (
        f"{mesh_object.name}: UV 루프 수가 메시 루프 수와 다릅니다."
    )
    assert any(edge.use_seam for edge in mesh_object.data.edges), (
        f"{mesh_object.name}: Seam이 하나도 생성되지 않았습니다."
    )
    for loop_index, uv_loop in enumerate(uv_layer.data):
        assert math.isfinite(uv_loop.uv.x) and math.isfinite(uv_loop.uv.y), (
            f"{mesh_object.name}: UV {loop_index}에 유한하지 않은 좌표가 있습니다."
        )


def _assert_existing_uv_reuse(mesh_object: bpy.types.Object) -> None:
    """UV 선택이 비어 있어도 기존 레이어 재사용이 성공하는지 검사합니다."""

    settings = bpy.context.scene.uvmapping_settings
    original_create_new = settings.create_new_uv_layer
    original_layer_name = settings.uv_layer_name
    original_sync = bpy.context.tool_settings.use_uv_select_sync
    try:
        _activate_only(mesh_object)
        settings.create_new_uv_layer = False
        settings.uv_layer_name = mesh_object.data.uv_layers.active.name
        bpy.context.tool_settings.use_uv_select_sync = False
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="DESELECT")
        bpy.ops.object.mode_set(mode="OBJECT")

        result = bpy.ops.uvmapping.auto_unwrap()
        assert "FINISHED" in result, f"기존 UV 레이어 재사용 실패: {result}"
        _assert_unwrapped(mesh_object)
        print("[smoke] 기존 UV 레이어와 비선택 UV 재사용 통과")
    finally:
        settings.create_new_uv_layer = original_create_new
        settings.uv_layer_name = original_layer_name
        bpy.context.tool_settings.use_uv_select_sync = original_sync


def main() -> None:
    _assert_isolated_profile()
    _clear_scene()

    module = importlib.import_module(MODULE_NAME)
    module.unregister()
    assert not _operator_registered(), "등록 해제 뒤에도 연산자가 남아 있습니다."

    module.register()
    assert _operator_registered(), f"연산자가 등록되지 않았습니다: {OPERATOR_ID}"

    mesh_objects = _create_meshes()
    try:
        for mesh_object in mesh_objects:
            _activate_only(mesh_object)
            result = bpy.ops.uvmapping.auto_unwrap()
            assert "FINISHED" in result, f"{mesh_object.name}: 자동 언랩 실패: {result}"
            _assert_unwrapped(mesh_object)
            print(
                f"[smoke] {mesh_object.name}: "
                f"faces={len(mesh_object.data.polygons)}, "
                f"seams={sum(edge.use_seam for edge in mesh_object.data.edges)}, "
                f"uv_loops={len(mesh_object.data.uv_layers.active.data)}"
            )
        _assert_existing_uv_reuse(mesh_objects[0])
    finally:
        _clear_scene()
        module.unregister()

    assert not _operator_registered(), "최종 등록 해제 뒤에도 연산자가 남아 있습니다."
    print("[smoke] 등록, 자동 언랩, UV/Seam/좌표, 등록 해제 검사 통과")


if __name__ == "__main__":
    main()
