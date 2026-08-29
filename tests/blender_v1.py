"""격리 Blender에서 v1 다중 객체·트랜잭션 동작을 검사합니다."""

from __future__ import annotations

import importlib
import json

import bpy


PREVIEW_ATTRIBUTE = "_uvmapping_preview_seam"
QUALITY_PROPERTY = "uvmapping_quality"
TEXTURE_JOB_PROPERTY = "uvmapping_texture_job"
MODULE_NAME = "bl_ext.user_default.uvmapping_blender"


def _auto_operator_registered() -> bool:
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


def _cube(name: str, location=(0.0, 0.0, 0.0)):
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=location)
    obj = bpy.context.object
    obj.name = name
    while obj.data.uv_layers:
        obj.data.uv_layers.remove(obj.data.uv_layers[0])
    for edge in obj.data.edges:
        edge.use_seam = False
    return obj


def _linked_copy(source, name: str, location):
    obj = source.copy()
    obj.data = source.data
    obj.name = name
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _select(objects, active=None) -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active or objects[0]


def _configure() -> None:
    settings = bpy.context.scene.uvmapping_settings
    settings.preset = "BALANCED"
    settings.quality_level = "FAST"
    settings.process_selected_objects = True
    settings.generate_texture_job = True
    settings.seam_policy = "REPLACE"
    settings.create_new_uv_layer = True
    settings.uv_layer_name = "AutoUV"
    settings.island_margin = 0.003


def _assert_result(obj) -> None:
    layer = obj.data.uv_layers.active
    assert layer is not None, f"{obj.name}: UV 레이어가 없습니다."
    assert len(layer.data) == len(obj.data.loops), f"{obj.name}: UV loop 수 불일치"
    assert any(edge.use_seam for edge in obj.data.edges), f"{obj.name}: Seam이 없습니다."
    quality = json.loads(obj[QUALITY_PROPERTY])
    texture_job = json.loads(obj[TEXTURE_JOB_PROPERTY])
    assert quality["triangle_count"] > 0, f"{obj.name}: 품질 삼각형 수가 없습니다."
    assert texture_job["schema_version"] == "1.2", f"{obj.name}: TextureJob 버전 오류"
    assert texture_job["object_name"] == obj.name_full, f"{obj.name}: 객체 이름 계약 오류"
    assert texture_job["mesh_hash"], f"{obj.name}: mesh hash가 없습니다."
    assert texture_job["target_resolution"] == [2048, 2048]
    assert texture_job["requested_padding"] == 16
    assert texture_job["requested_udim_tiles"] == [1001]
    assert texture_job["packing_margin_method"] == "SCALED"
    assert abs(texture_job["packing_margin_uv"] - 0.003) < 1.0e-6
    contract_settings = texture_job["settings"]
    assert contract_settings["packing_margin_method"] == "SCALED"
    assert abs(contract_settings["packing_margin_uv"] - 0.003) < 1.0e-6
    generation_target = contract_settings["generation_target"]
    assert generation_target["resolution"] == [2048, 2048]
    assert generation_target["padding_pixels"] == 16
    assert generation_target["udim_tiles"] == [1001]


def _test_register_rollback() -> None:
    _clear_scene()
    module = importlib.import_module(MODULE_NAME)
    module.unregister()
    original_register_class = bpy.utils.register_class
    calls = 0

    def fail_second_registration(cls):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("테스트용 클래스 등록 실패")
        return original_register_class(cls)

    bpy.utils.register_class = fail_second_registration
    try:
        try:
            module.register()
        except RuntimeError as exc:
            assert "테스트용 클래스 등록 실패" in str(exc)
        else:
            raise AssertionError("테스트용 등록 실패가 발생하지 않았습니다.")
    finally:
        bpy.utils.register_class = original_register_class

    assert not hasattr(bpy.types.Scene, "uvmapping_settings")
    assert not _auto_operator_registered(), "등록 실패 뒤 연산자 클래스가 남았습니다."
    module.register()
    assert hasattr(bpy.types.Scene, "uvmapping_settings")
    assert _auto_operator_registered(), "등록 롤백 뒤 정상 재등록에 실패했습니다."
    print("[v1] register 중간 실패의 클래스와 Scene 속성 롤백 통과")


def _test_independent_and_context_restore() -> None:
    _clear_scene()
    first = _cube("IndependentA", (-2.0, 0.0, 0.0))
    second = _cube("IndependentB", (2.0, 0.0, 0.0))
    first_mesh_pointer = first.data.as_pointer()
    second_mesh_pointer = second.data.as_pointer()
    _select((first, second), active=second)

    bpy.context.tool_settings.mesh_select_mode = (False, True, False)
    bpy.context.tool_settings.use_uv_select_sync = False
    original_uv_mode = bpy.context.tool_settings.uv_select_mode
    bpy.ops.object.mode_set(mode="EDIT")
    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"독립 Mesh 다중 처리 실패: {result}"
    assert bpy.context.view_layer.objects.active == second, "활성 객체가 복원되지 않았습니다."
    assert second.mode == "EDIT", "원래 Edit Mode가 복원되지 않았습니다."
    assert tuple(bpy.context.tool_settings.mesh_select_mode) == (False, True, False)
    assert bpy.context.tool_settings.use_uv_select_sync is False
    assert bpy.context.tool_settings.uv_select_mode == original_uv_mode
    bpy.ops.object.mode_set(mode="OBJECT")

    assert first.data.as_pointer() != first_mesh_pointer
    assert second.data.as_pointer() != second_mesh_pointer
    assert first.data != second.data
    _assert_result(first)
    _assert_result(second)
    print("[v1] 독립 Mesh 다중 처리와 Edit Mode 상태 복원 통과")


def _test_shared_all_selected() -> None:
    _clear_scene()
    first = _cube("SharedAllA")
    second = _linked_copy(first, "SharedAllB", (3.0, 0.0, 0.0))
    original_pointer = first.data.as_pointer()
    _select((first, second), active=first)
    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"전체 공유 Mesh 처리 실패: {result}"
    assert first.data == second.data, "전체 선택 공유 관계가 유지되지 않았습니다."
    assert first.data.as_pointer() != original_pointer, "winner datablock으로 교체되지 않았습니다."
    _assert_result(first)
    _assert_result(second)
    print("[v1] 공유 Mesh 전체 선택 처리 통과")


def _test_shared_partial_selection() -> None:
    _clear_scene()
    first = _cube("SharedPartialA")
    second = _linked_copy(first, "SharedPartialB", (3.0, 0.0, 0.0))
    untouched = _linked_copy(first, "SharedPartialUntouched", (6.0, 0.0, 0.0))
    original_mesh = untouched.data
    original_seams = tuple(edge.use_seam for edge in original_mesh.edges)
    _select((first, second), active=first)
    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"부분 공유 Mesh 처리 실패: {result}"
    assert first.data == second.data, "선택 객체끼리 winner를 공유하지 않습니다."
    assert first.data != untouched.data, "미선택 user까지 winner로 바뀌었습니다."
    assert untouched.data == original_mesh, "미선택 user의 원본 binding이 바뀌었습니다."
    assert tuple(edge.use_seam for edge in untouched.data.edges) == original_seams
    assert len(untouched.data.uv_layers) == 0, "미선택 user 원본 UV가 바뀌었습니다."
    _assert_result(first)
    _assert_result(second)
    print("[v1] 공유 Mesh 부분 선택의 선택 대상 분리 통과")


def _test_preview_does_not_change_seams() -> None:
    _clear_scene()
    obj = _cube("Preview")
    obj.data.edges[0].use_seam = True
    original = tuple(edge.use_seam for edge in obj.data.edges)
    _select((obj,))
    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}, f"미리보기 실패: {result}"
    assert tuple(edge.use_seam for edge in obj.data.edges) == original
    attribute = obj.data.attributes.get(PREVIEW_ATTRIBUTE)
    assert attribute is not None and attribute.domain == "EDGE"
    values = [item.value for item in attribute.data]
    assert any(value > 0.5 for value in values), "미리보기 후보 값이 없습니다."

    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"미리보기 뒤 자동 언랩 실패: {result}"
    assert obj.data.attributes.get(PREVIEW_ATTRIBUTE) is None, (
        "winner 결과 Mesh에 미리보기 속성이 복제됐습니다."
    )
    _assert_result(obj)

    seams_after_unwrap = tuple(edge.use_seam for edge in obj.data.edges)
    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}, f"두 번째 미리보기 실패: {result}"
    assert tuple(edge.use_seam for edge in obj.data.edges) == seams_after_unwrap
    result = bpy.ops.uvmapping.clear_preview()
    assert result == {"FINISHED"}, f"미리보기 지우기 실패: {result}"
    assert obj.data.attributes.get(PREVIEW_ATTRIBUTE) is None
    assert tuple(edge.use_seam for edge in obj.data.edges) == seams_after_unwrap
    print("[v1] Seam 불변 미리보기, winner 속성 제거와 Clear 통과")


def _test_finalize_failure_does_not_rollback() -> None:
    _clear_scene()
    obj = _cube("FinalizeFailure")
    original_mesh = obj.data
    _select((obj,))
    operators = importlib.import_module(f"{MODULE_NAME}.uvmapping.operators")
    original_remove = operators._remove_orphan_mesh

    def fail_cleanup(_mesh):
        raise RuntimeError("테스트용 finalize 정리 실패")

    operators._remove_orphan_mesh = fail_cleanup
    try:
        result = bpy.ops.uvmapping.auto_unwrap()
    finally:
        operators._remove_orphan_mesh = original_remove
    assert result == {"FINISHED"}, f"finalize 정리 실패가 commit을 취소했습니다: {result}"
    assert obj.data != original_mesh, "성공한 winner binding이 rollback됐습니다."
    _assert_result(obj)
    print("[v1] 성공 commit 이후 finalize 실패의 비롤백 처리 통과")


def _test_commit_rollback_injection() -> None:
    _clear_scene()
    first = _cube("RollbackA", (-2.0, 0.0, 0.0))
    second = _cube("RollbackB", (2.0, 0.0, 0.0))
    original = {
        first.as_pointer(): (
            first.data,
            tuple(edge.use_seam for edge in first.data.edges),
            len(first.data.uv_layers),
        ),
        second.as_pointer(): (
            second.data,
            tuple(edge.use_seam for edge in second.data.edges),
            len(second.data.uv_layers),
        ),
    }
    _select((first, second), active=first)
    try:
        result = bpy.ops.uvmapping.auto_unwrap(debug_fail_after_bindings=1)
    except RuntimeError as exc:
        assert "테스트용 commit 실패" in str(exc), f"예상하지 못한 오류: {exc}"
        result = {"CANCELLED"}
    assert result == {"CANCELLED"}, f"주입 실패가 취소되지 않았습니다: {result}"
    for obj in (first, second):
        mesh, seams, uv_count = original[obj.as_pointer()]
        assert obj.data == mesh, f"{obj.name}: binding이 롤백되지 않았습니다."
        assert tuple(edge.use_seam for edge in obj.data.edges) == seams
        assert len(obj.data.uv_layers) == uv_count
        assert QUALITY_PROPERTY not in obj and TEXTURE_JOB_PROPERTY not in obj
    assert bpy.context.view_layer.objects.active == first
    assert first.select_get() and second.select_get()
    print("[v1] commit 실패 주입의 전체 binding/속성 롤백 통과")


def main() -> None:
    _test_register_rollback()
    _configure()
    try:
        _test_independent_and_context_restore()
        _test_shared_all_selected()
        _test_shared_partial_selection()
        _test_preview_does_not_change_seams()
        _test_finalize_failure_does_not_rollback()
        _test_commit_rollback_injection()
    finally:
        _clear_scene()
    print("[v1] 전체 통합 회귀 통과")


if __name__ == "__main__":
    main()
