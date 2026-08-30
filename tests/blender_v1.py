"""격리 Blender에서 v1 다중 객체·트랜잭션 동작을 검사합니다."""

from __future__ import annotations

import importlib
import json

import bpy


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


def _plane(name: str, location=(0.0, 0.0, 0.0)):
    bpy.ops.mesh.primitive_plane_add(size=2.0, location=location)
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
    settings.generate_texture_job = True
    settings.seam_policy = "REPLACE"
    settings.create_new_uv_layer = True
    settings.uv_layer_name = "AutoUV"
    settings.island_margin = 0.003
    settings.texture_resolution = "1024"
    settings.padding_pixels = 12
    settings.pack_shared_atlas = True


def _test_sidebar_contract() -> None:
    settings = bpy.context.scene.uvmapping_settings
    properties = settings.bl_rna.properties
    assert properties.get("process_selected_objects") is None, (
        "선택 객체 처리 체크박스 속성이 남아 있습니다."
    )
    resolution_items = {
        item.identifier
        for item in properties["texture_resolution"].enum_items
    }
    assert {"256", "512", "1024", "2048", "4096", "8192"} <= resolution_items
    assert properties["padding_pixels"].name == "UV 패딩"
    assert properties["padding_pixels"].description == (
        "각 UV 조각 가장자리에 확보할 여백을 픽셀 단위로 정합니다"
    )

    original_resolution = settings.texture_resolution
    try:
        for resolution in ("256", "512"):
            settings.texture_resolution = resolution
            expected_margin = settings.padding_pixels / int(resolution)
            assert abs(settings.island_margin - expected_margin) < 1.0e-9
    finally:
        settings.texture_resolution = original_resolution

    operators = importlib.import_module(f"{MODULE_NAME}.uvmapping.operators")
    ui = importlib.import_module(f"{MODULE_NAME}.uvmapping.ui")
    assert ui.UVMAPPING_PT_main.bl_label == "UV 언랩"
    assert operators.UVMAPPING_OT_auto_unwrap.bl_idname == "uvmapping.auto_unwrap"
    assert operators.UVMAPPING_OT_auto_unwrap.bl_label == "UV 언랩"
    assert operators.UVMAPPING_OT_preview_seams.bl_label == "Seam 보기"
    assert operators.UVMAPPING_OT_preview_seams.bl_description == (
        "선택한 메시에서 자동 생성될 Seam 위치를 미리 표시합니다"
    )
    assert operators.UVMAPPING_OT_preview_seams.bl_options == {"REGISTER"}
    assert operators.UVMAPPING_OT_clear_preview.bl_label == "Seam 숨기기"
    assert operators.UVMAPPING_OT_clear_preview.bl_description == (
        "Seam 미리보기를 숨깁니다"
    )
    assert operators.UVMAPPING_OT_clear_preview.bl_options == {"REGISTER"}
    print("[v1] 사이드바 라벨, 툴팁, 256/512 해상도 계약 통과")


def _test_selected_meshes_only() -> None:
    _clear_scene()
    preview = importlib.import_module(f"{MODULE_NAME}.uvmapping.preview")
    selected = _cube("SelectedTarget", (-2.0, 0.0, 0.0))
    untouched = _cube("UnselectedTarget", (2.0, 0.0, 0.0))
    selected_mesh = selected.data
    untouched_mesh = untouched.data
    untouched_seams = tuple(edge.use_seam for edge in untouched.data.edges)
    untouched_uv_count = len(untouched.data.uv_layers)
    selected_attributes = tuple(attribute.name for attribute in selected.data.attributes)
    untouched_attributes = tuple(attribute.name for attribute in untouched.data.attributes)
    _select((selected,), active=selected)

    result = bpy.ops.uvmapping.analyze()
    assert result == {"FINISHED"}, f"선택 대상 분석 실패: {result}"
    assert "객체 1개/메시 1개" in bpy.context.scene.uvmapping_settings.last_result
    assert untouched.data == untouched_mesh
    assert len(untouched.data.uv_layers) == untouched_uv_count

    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}, f"선택 대상 Seam 보기 실패: {result}"
    assert preview.mesh_edge_indices(selected_mesh)
    assert not preview.mesh_edge_indices(untouched_mesh)
    assert tuple(attribute.name for attribute in selected.data.attributes) == selected_attributes
    assert tuple(attribute.name for attribute in untouched.data.attributes) == untouched_attributes

    result = bpy.ops.uvmapping.clear_preview()
    assert result == {"FINISHED"}, f"선택 대상 Seam 숨기기 실패: {result}"
    assert not preview.mesh_edge_indices(selected_mesh)
    assert not preview.mesh_edge_indices(untouched_mesh)

    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"선택 대상 UV 언랩 실패: {result}"
    _assert_result(selected)
    assert selected.data != untouched_mesh
    assert untouched.data == untouched_mesh, "미선택 객체의 Mesh binding이 바뀌었습니다."
    assert tuple(edge.use_seam for edge in untouched.data.edges) == untouched_seams
    assert len(untouched.data.uv_layers) == untouched_uv_count
    assert tuple(attribute.name for attribute in untouched.data.attributes) == untouched_attributes
    assert selected.select_get() and not untouched.select_get()
    print("[v1] 분석, Seam 보기/숨기기, UV 언랩의 미선택 Mesh 제외 통과")


def _test_preview_overlay_registry_and_stale_cleanup() -> None:
    _clear_scene()
    preview = importlib.import_module(f"{MODULE_NAME}.uvmapping.preview")
    ui = importlib.import_module(f"{MODULE_NAME}.uvmapping.ui")
    selected = _cube("OverlaySelected")
    unselected_user = _linked_copy(selected, "OverlayUnselected", (3.0, 0.0, 0.0))
    _select((selected,), active=selected)

    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}, f"공유 Mesh Seam 보기 실패: {result}"
    assert preview.is_active() and preview.draw_handler_active()
    assert preview.target_count() == 1
    assert preview.target_object_uids() == (selected.session_uid,)
    assert unselected_user.session_uid not in preview.target_object_uids()
    assert preview.mesh_edge_indices(selected.data)
    original_attributes = tuple(
        attribute.name for attribute in selected.data.attributes
    )

    original_get_shader = preview._get_shader
    original_batch_for_shader = preview.batch_for_shader
    preview._get_shader = lambda: object()
    preview.batch_for_shader = lambda _shader, primitive, content: (
        primitive,
        tuple(content["pos"]),
    )
    try:
        first_batch = preview._batch_for_mesh(selected.data)
        second_batch = preview._batch_for_mesh(selected.data)
        assert first_batch is not None and first_batch is second_batch
        assert preview.batch_cache_size() == 1
    finally:
        preview._get_shader = original_get_shader
        preview.batch_for_shader = original_batch_for_shader
        preview._batch_cache.clear()
    preview._draw_callback()

    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = None
    assert ui.UVMAPPING_PT_main.poll(bpy.context), (
        "활성 미리보기 중 선택이 없으면 패널이 숨겨졌습니다."
    )
    assert bpy.ops.uvmapping.clear_preview.poll(), (
        "선택 변경 뒤 Seam 숨기기를 실행할 수 없습니다."
    )
    result = bpy.ops.uvmapping.clear_preview()
    assert result == {"FINISHED"}, f"선택 변경 뒤 Seam 숨기기 실패: {result}"
    assert not preview.mesh_edge_indices(selected.data)
    assert tuple(attribute.name for attribute in selected.data.attributes) == original_attributes
    assert not preview.is_active() and not preview.draw_handler_active()
    assert preview.batch_cache_size() == 0

    _select((selected, unselected_user), active=selected)
    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}
    shared_mesh = selected.data
    assert preview.target_count() == 2
    assert preview.has_mesh_targets(shared_mesh)
    shared_edges = preview.mesh_edge_indices(shared_mesh)
    assert shared_edges

    selected.name = "OverlayRenamed"
    assert preview.prune_stale_targets() == 0
    renamed_target = preview._targets[selected.session_uid]
    assert renamed_target.object_name == selected.name_full

    selected.data = shared_mesh.copy()
    assert preview.prune_stale_targets() == 1
    assert preview.target_count() == 1
    assert preview.target_object_uids() == (unselected_user.session_uid,)
    assert preview.has_mesh_targets(shared_mesh)
    assert preview.mesh_edge_indices(shared_mesh) == shared_edges

    preview._history_update()
    assert preview.target_count() == 1, "Undo 경계가 runtime target을 지웠습니다."
    assert preview.has_mesh_targets(shared_mesh)
    assert preview.mesh_edge_indices(shared_mesh) == shared_edges

    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = None
    result = bpy.ops.uvmapping.clear_preview()
    assert result == {"FINISHED"}
    assert not preview.mesh_edge_indices(shared_mesh)
    assert not preview.is_active() and not preview.draw_handler_active()
    preview._draw_callback()
    print(
        "[v1] GPU overlay registry, O(1) 이름 재해결, 공유 stale와 Undo tombstone 통과"
    )


def _test_partial_shared_preview_survives_other_user_unwrap() -> None:
    _clear_scene()
    preview = importlib.import_module(f"{MODULE_NAME}.uvmapping.preview")
    first = _cube("PreviewPartialA")
    second = _linked_copy(first, "PreviewPartialB", (3.0, 0.0, 0.0))
    shared_mesh = first.data
    _select((first, second), active=first)

    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}
    assert preview.target_count() == 2
    shared_edges = preview.mesh_edge_indices(shared_mesh)
    assert shared_edges
    original_attributes = tuple(
        attribute.name for attribute in shared_mesh.attributes
    )

    _select((first,), active=first)
    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"부분 공유 미리보기 뒤 UV 언랩 실패: {result}"
    _assert_result(first)
    assert first.data != shared_mesh
    assert not preview.mesh_edge_indices(first.data)
    assert second.data == shared_mesh
    assert preview.mesh_edge_indices(shared_mesh) == shared_edges
    assert preview.target_object_uids() == (second.session_uid,)
    assert preview.has_mesh_targets(shared_mesh)
    assert tuple(attribute.name for attribute in shared_mesh.attributes) == original_attributes

    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = None
    result = bpy.ops.uvmapping.clear_preview()
    assert result == {"FINISHED"}
    assert not preview.mesh_edge_indices(shared_mesh)
    assert not preview.is_active()
    print("[v1] 공유 Mesh 한 user 언랩 뒤 다른 user 미리보기 보존 통과")


def _test_padding_boundaries_and_small_resolutions() -> None:
    settings = bpy.context.scene.uvmapping_settings
    operators = importlib.import_module(f"{MODULE_NAME}.uvmapping.operators")
    original_resolution = settings.texture_resolution
    original_padding = settings.padding_pixels
    original_shared = settings.pack_shared_atlas
    try:
        settings.texture_resolution = "256"
        settings.padding_pixels = 127
        assert operators._atlas_margin(settings) == (256, 127, 127 / 256)

        invalid_cases = (
            ("256", 128, 127),
            ("256", 256, 127),
            ("512", 256, 255),
        )
        for resolution, padding, maximum in invalid_cases:
            _clear_scene()
            obj = _cube(f"InvalidPadding{resolution}_{padding}")
            original_mesh = obj.data
            original_seams = tuple(edge.use_seam for edge in obj.data.edges)
            _select((obj,), active=obj)
            settings.texture_resolution = resolution
            settings.padding_pixels = padding
            try:
                result = bpy.ops.uvmapping.auto_unwrap()
            except RuntimeError as exc:
                assert f"{maximum}px 이하여야 합니다" in str(exc)
                result = {"CANCELLED"}
            assert result == {"CANCELLED"}, (
                f"{resolution}/{padding}px 패딩이 거부되지 않았습니다: {result}"
            )
            assert f"{maximum}px 이하여야 합니다" in settings.last_result
            assert obj.data == original_mesh
            assert tuple(edge.use_seam for edge in obj.data.edges) == original_seams
            assert len(obj.data.uv_layers) == 0
            assert QUALITY_PROPERTY not in obj and TEXTURE_JOB_PROPERTY not in obj

        for resolution in ("256", "512"):
            for shared in (True, False):
                _clear_scene()
                first = _plane(f"Small{resolution}_{shared}A", (-2.0, 0.0, 0.0))
                second = _plane(f"Small{resolution}_{shared}B", (2.0, 0.0, 0.0))
                _select((first, second), active=first)
                settings.texture_resolution = resolution
                settings.padding_pixels = 16
                settings.pack_shared_atlas = shared
                result = bpy.ops.uvmapping.auto_unwrap()
                assert result == {"FINISHED"}, (
                    f"{resolution}/16px shared={shared} 패킹 실패: {result}"
                )
                for obj in (first, second):
                    _assert_result(obj)
                    job = _texture_job(obj)
                    assert job["target_resolution"] == [int(resolution)] * 2
                    assert job["requested_padding"] == 16
                    assert job["packing_margin_method"] == "FRACTION"
                    assert abs(job["packing_margin_uv"] - 16 / int(resolution)) < 1.0e-9
                    assert job["pack_shared_atlas"] is shared
                if shared:
                    _assert_shared_atlas_contract(first, second)
                    assert _cross_object_overlap_count(first, second) == 0
                else:
                    assert _texture_job(first)["atlas_id"] != _texture_job(second)["atlas_id"]
    finally:
        settings.texture_resolution = original_resolution
        settings.padding_pixels = original_padding
        settings.pack_shared_atlas = original_shared
        _clear_scene()
    print("[v1] 256/512 해상도 패딩 경계와 실제 Atlas 계약 통과")


def _assert_result(obj) -> None:
    layer = obj.data.uv_layers.active
    assert layer is not None, f"{obj.name}: UV 레이어가 없습니다."
    assert len(layer.data) == len(obj.data.loops), f"{obj.name}: UV loop 수 불일치"
    assert any(edge.use_seam for edge in obj.data.edges), f"{obj.name}: Seam이 없습니다."
    quality = json.loads(obj[QUALITY_PROPERTY])
    texture_job = json.loads(obj[TEXTURE_JOB_PROPERTY])
    assert quality["triangle_count"] > 0, f"{obj.name}: 품질 삼각형 수가 없습니다."
    assert texture_job["schema_version"] == "1.4", f"{obj.name}: TextureJob 버전 오류"
    assert texture_job["object_name"] == obj.name_full, f"{obj.name}: 객체 이름 계약 오류"
    assert texture_job["mesh_hash"], f"{obj.name}: mesh hash가 없습니다."
    settings = bpy.context.scene.uvmapping_settings
    resolution = int(settings.texture_resolution)
    padding = int(settings.padding_pixels)
    margin = padding / resolution
    assert texture_job["target_resolution"] == [resolution, resolution]
    assert texture_job["requested_padding"] == padding
    assert texture_job["requested_udim_tiles"] == [1001]
    assert texture_job["packing_margin_method"] == "FRACTION"
    assert abs(texture_job["packing_margin_uv"] - margin) < 1.0e-6
    contract_settings = texture_job["settings"]
    assert contract_settings["packing_margin_method"] == "FRACTION"
    assert abs(contract_settings["packing_margin_uv"] - margin) < 1.0e-6
    assert contract_settings["pack_shared_atlas"] == settings.pack_shared_atlas
    generation_target = contract_settings["generation_target"]
    assert generation_target["resolution"] == [resolution, resolution]
    assert generation_target["padding_pixels"] == padding
    assert generation_target["udim_tiles"] == [1001]
    assert texture_job["atlas_id"], f"{obj.name}: atlas ID가 없습니다."
    assert texture_job["atlas_hash"], f"{obj.name}: atlas hash가 없습니다."
    assert texture_job["atlas_member_id"], f"{obj.name}: atlas member ID가 없습니다."
    assert texture_job["atlas_overlap_status"] == "EXACT"
    assert texture_job["atlas_overlap_count"] == 0
    assert texture_job["atlas_valid"] is True
    assert texture_job["atlas_out_of_bounds_count"] == 0
    assert texture_job["atlas_member_count"] >= 1
    assert texture_job["atlas_triangle_count"] > 0
    atlas_bounds = texture_job["atlas_bounds"]
    assert 0.0 <= atlas_bounds[0][0] <= atlas_bounds[1][0] <= 1.0
    assert 0.0 <= atlas_bounds[0][1] <= atlas_bounds[1][1] <= 1.0
    assert texture_job["atlas_members"], f"{obj.name}: atlas member manifest가 없습니다."
    assert all("global_island_id" in island for island in texture_job["islands"])
    _assert_no_atlas_proxies()


def _assert_no_atlas_proxies() -> None:
    proxies = [
        obj.name_full
        for obj in bpy.data.objects
        if obj.name.startswith("_UVMappingAtlas_")
        or bool(obj.get("_uvmapping_atlas_proxy", False))
    ]
    assert not proxies, f"Atlas 임시 proxy가 남았습니다: {proxies}"


def _texture_job(obj):
    return json.loads(obj[TEXTURE_JOB_PROPERTY])


def _assert_shared_atlas_contract(first, second) -> None:
    first_job = _texture_job(first)
    second_job = _texture_job(second)
    assert first_job["atlas_id"] == second_job["atlas_id"]
    assert first_job["atlas_hash"] == second_job["atlas_hash"]
    assert first_job["atlas_members"] == second_job["atlas_members"]
    assert len(first_job["atlas_members"]) == 2
    assert first_job["atlas_member_id"] != second_job["atlas_member_id"]
    assert first_job["pack_shared_atlas"] is True
    assert second_job["pack_shared_atlas"] is True
    for member in first_job["atlas_members"]:
        assert member["mesh_name"]
        assert member["object_names"]
        assert "object_name" not in member
    first_global_ids = {
        island["global_island_id"] for island in first_job["islands"]
    }
    second_global_ids = {
        island["global_island_id"] for island in second_job["islands"]
    }
    assert first_global_ids
    assert second_global_ids
    assert first_global_ids.isdisjoint(second_global_ids)


def _uv_triangles(obj):
    quality = importlib.import_module(f"{MODULE_NAME}.uvmapping.quality")
    layer = obj.data.uv_layers.active
    _, faces, _ = quality._extract_faces(obj.data, layer.name)
    return quality._triangulate(faces)


def _cross_object_overlap_count(first, second) -> int:
    quality = importlib.import_module(f"{MODULE_NAME}.uvmapping.quality")
    return sum(
        quality._triangle_intersection_area(first_triangle.uvs, second_triangle.uvs)
        > 1.0e-10
        for first_triangle in _uv_triangles(first)
        for second_triangle in _uv_triangles(second)
    )


def _assert_uv_unit_bounds(obj) -> None:
    layer = obj.data.uv_layers.active
    for loop in layer.data:
        assert -1.0e-6 <= loop.uv.x <= 1.0 + 1.0e-6
        assert -1.0e-6 <= loop.uv.y <= 1.0 + 1.0e-6


def _uv_bbox(obj):
    layer = obj.data.uv_layers.active
    values = [(float(loop.uv.x), float(loop.uv.y)) for loop in layer.data]
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[0] for value in values),
        max(value[1] for value in values),
    )


def _bbox_separation(first, second) -> float:
    return max(
        second[0] - first[2],
        first[0] - second[2],
        second[1] - first[3],
        first[1] - second[3],
    )


def _test_register_rollback() -> None:
    _clear_scene()
    module = importlib.import_module(MODULE_NAME)
    preview = importlib.import_module(f"{MODULE_NAME}.uvmapping.preview")
    module.unregister()
    assert not preview.is_registered()
    assert not preview.draw_handler_active()
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
    assert preview.is_registered()
    preview.register()
    preview.register()
    for handlers, callback in preview._HANDLERS:
        assert handlers.count(callback) == 1, "미리보기 수명 주기 handler가 중복됐습니다."
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
    _assert_uv_unit_bounds(first)
    _assert_uv_unit_bounds(second)
    assert _cross_object_overlap_count(first, second) == 0, (
        "공유 아틀라스의 서로 다른 Mesh UV가 겹칩니다."
    )
    _assert_shared_atlas_contract(first, second)
    print("[v1] 독립 Mesh 다중 처리와 Edit Mode 상태 복원 통과")


def _test_uv_select_mode_restore_guard() -> None:
    operators = importlib.import_module(f"{MODULE_NAME}.uvmapping.operators")

    class TrackingToolSettings:
        def __init__(self, uv_select_mode):
            self.mesh_select_mode = (True, False, False)
            self.use_uv_select_sync = False
            self._uv_select_mode = uv_select_mode
            self.uv_select_mode_writes = []

        @property
        def uv_select_mode(self):
            return self._uv_select_mode

        @uv_select_mode.setter
        def uv_select_mode(self, value):
            self.uv_select_mode_writes.append(value)
            self._uv_select_mode = value

    class EmptyObjects:
        active = None

        def __iter__(self):
            return iter(())

    class FakeContext:
        def __init__(self, tool_settings):
            self.tool_settings = tool_settings
            self.view_layer = type("FakeViewLayer", (), {"objects": EmptyObjects()})()

    def restored_state(current_mode, captured_mode):
        tool_settings = TrackingToolSettings(current_mode)
        state = operators._ContextState.__new__(operators._ContextState)
        state.context = FakeContext(tool_settings)
        state.targets = ()
        state.active_object = None
        state.active_mode = "OBJECT"
        state.objects_in_mode = ()
        state.object_selection = ()
        state.mesh_select_mode = (True, False, False)
        state.use_uv_select_sync = False
        state.uv_select_mode = captured_mode
        state.mesh_states = {}
        state.mesh_replacements = {}
        state.prepared = True
        state.restore()
        return tool_settings

    unchanged = restored_state("VERTEX", "VERTEX")
    assert unchanged.uv_select_mode_writes == [], (
        "동일한 UV 선택 모드를 불필요하게 다시 설정했습니다."
    )

    changed = restored_state("EDGE", "VERTEX")
    assert changed.uv_select_mode == "VERTEX"
    assert changed.uv_select_mode_writes == ["VERTEX"]
    print("[v1] UV 선택 모드의 조건부 상태 복원 통과")


def _test_mesh_selection_restore_avoids_uv_loop_data() -> None:
    operators = importlib.import_module(f"{MODULE_NAME}.uvmapping.operators")

    class SelectionItem:
        def __init__(self):
            self.select = False

    class ForbiddenUVLayers:
        def __iter__(self):
            raise AssertionError("교체 Mesh의 UV loop 선택 데이터에 접근했습니다.")

    class FakeMesh:
        def __init__(self):
            self.vertices = [SelectionItem(), SelectionItem()]
            self.edges = [SelectionItem()]
            self.polygons = [SelectionItem()]
            self.uv_layers = ForbiddenUVLayers()
            self.update_count = 0

        def update(self):
            self.update_count += 1

    mesh = FakeMesh()
    operators._restore_mesh_selection(
        mesh,
        {
            "vertices": (True, False),
            "edges": (True,),
            "polygons": (True,),
        },
    )
    assert [item.select for item in mesh.vertices] == [True, False]
    assert [item.select for item in mesh.edges] == [True]
    assert [item.select for item in mesh.polygons] == [True]
    assert mesh.update_count == 1
    print("[v1] Mesh 선택 복원 중 UV loop 데이터 비접근 통과")


def _test_independent_pack_mode() -> None:
    _clear_scene()
    settings = bpy.context.scene.uvmapping_settings
    settings.pack_shared_atlas = False
    try:
        first = _cube("IndependentPackA", (-2.0, 0.0, 0.0))
        second = _cube("IndependentPackB", (2.0, 0.0, 0.0))
        _select((first, second), active=first)
        result = bpy.ops.uvmapping.auto_unwrap()
        assert result == {"FINISHED"}, f"독립 패킹 실패: {result}"
        _assert_result(first)
        _assert_result(second)
        _assert_uv_unit_bounds(first)
        _assert_uv_unit_bounds(second)
        first_job = _texture_job(first)
        second_job = _texture_job(second)
        assert first_job["atlas_id"] != second_job["atlas_id"]
        assert first_job["atlas_member_id"] != second_job["atlas_member_id"]
        assert first_job["pack_shared_atlas"] is False
        assert second_job["pack_shared_atlas"] is False
        assert len(first_job["atlas_members"]) == 1
        assert len(second_job["atlas_members"]) == 1
        assert first_job["atlas_members"][0]["bounds"]
        assert second_job["atlas_members"][0]["bounds"]
    finally:
        settings.pack_shared_atlas = True
    print("[v1] 독립 FRACTION 패킹 통과")


def _test_shared_atlas_pixel_padding() -> None:
    _clear_scene()
    settings = bpy.context.scene.uvmapping_settings
    assert settings.texture_resolution == "1024"
    assert settings.padding_pixels == 12
    assert settings.pack_shared_atlas is True
    first = _plane("AtlasPaddingA", (-2.0, 0.0, 0.0))
    second = _plane("AtlasPaddingB", (2.0, 0.0, 0.0))
    _select((first, second), active=first)

    result = bpy.ops.uvmapping.auto_unwrap()

    assert result == {"FINISHED"}, f"Plane 공유 Atlas 패킹 실패: {result}"
    _assert_result(first)
    _assert_result(second)
    _assert_uv_unit_bounds(first)
    _assert_uv_unit_bounds(second)
    assert _cross_object_overlap_count(first, second) == 0
    _assert_shared_atlas_contract(first, second)
    separation_pixels = _bbox_separation(
        _uv_bbox(first), _uv_bbox(second)
    ) * int(settings.texture_resolution)
    assert separation_pixels >= settings.padding_pixels * 2 - 1.5, (
        f"Atlas 아일랜드 간격이 부족합니다: {separation_pixels:.3f}px"
    )
    print(
        f"[v1] 공유 Atlas 1024/12px 패딩 통과: {separation_pixels:.3f}px"
    )


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
    preview = importlib.import_module(f"{MODULE_NAME}.uvmapping.preview")
    obj = _cube("Preview")
    obj.data.edges[0].use_seam = True
    original = tuple(edge.use_seam for edge in obj.data.edges)
    _select((obj,))
    bpy.ops.object.mode_set(mode="EDIT")
    edit_attributes = tuple(attribute.name for attribute in obj.data.attributes)
    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}, f"미리보기 실패: {result}"
    assert obj.mode == "EDIT", "Seam 보기가 원래 Edit Mode를 복원하지 않았습니다."
    assert obj.select_get(), "Seam 보기가 객체 선택을 바꿨습니다."
    assert tuple(attribute.name for attribute in obj.data.attributes) == edit_attributes
    bpy.ops.object.mode_set(mode="OBJECT")
    assert tuple(edge.use_seam for edge in obj.data.edges) == original
    assert preview.mesh_edge_indices(obj.data), "런타임 미리보기 후보가 없습니다."

    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"미리보기 뒤 UV 언랩 실패: {result}"
    assert not preview.mesh_edge_indices(obj.data)
    _assert_result(obj)

    seams_after_unwrap = tuple(edge.use_seam for edge in obj.data.edges)
    attributes_after_unwrap = tuple(attribute.name for attribute in obj.data.attributes)
    result = bpy.ops.uvmapping.preview_seams()
    assert result == {"FINISHED"}, f"두 번째 미리보기 실패: {result}"
    assert tuple(edge.use_seam for edge in obj.data.edges) == seams_after_unwrap
    assert tuple(attribute.name for attribute in obj.data.attributes) == attributes_after_unwrap
    assert preview.mesh_edge_indices(obj.data)
    result = bpy.ops.uvmapping.clear_preview()
    assert result == {"FINISHED"}, f"Seam 숨기기 실패: {result}"
    assert not preview.mesh_edge_indices(obj.data)
    assert tuple(edge.use_seam for edge in obj.data.edges) == seams_after_unwrap
    assert tuple(attribute.name for attribute in obj.data.attributes) == attributes_after_unwrap
    print("[v1] Seam/attribute 불변 런타임 미리보기와 숨기기 통과")


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


def _test_atlas_pack_failure_cleanup() -> None:
    _clear_scene()
    first = _cube("AtlasFailureA", (-2.0, 0.0, 0.0))
    second = _cube("AtlasFailureB", (2.0, 0.0, 0.0))
    originals = {first.as_pointer(): first.data, second.as_pointer(): second.data}
    _select((first, second), active=first)

    try:
        result = bpy.ops.uvmapping.auto_unwrap(debug_fail_atlas_pack=1)
    except RuntimeError as exc:
        assert "테스트용 Atlas 패킹 실패" in str(exc), f"예상하지 못한 오류: {exc}"
        result = {"CANCELLED"}

    assert result == {"CANCELLED"}, f"Atlas 실패 주입이 취소되지 않았습니다: {result}"
    for obj in (first, second):
        assert obj.data == originals[obj.as_pointer()]
        assert QUALITY_PROPERTY not in obj
        assert TEXTURE_JOB_PROPERTY not in obj
    assert bpy.context.view_layer.objects.active == first
    assert first.select_get() and second.select_get()
    _assert_no_atlas_proxies()
    print("[v1] Atlas 패킹 실패의 원본 불변과 proxy 정리 통과")


def main() -> None:
    _test_register_rollback()
    _configure()
    try:
        _test_sidebar_contract()
        _test_selected_meshes_only()
        _test_preview_overlay_registry_and_stale_cleanup()
        _test_partial_shared_preview_survives_other_user_unwrap()
        _test_padding_boundaries_and_small_resolutions()
        _test_uv_select_mode_restore_guard()
        _test_mesh_selection_restore_avoids_uv_loop_data()
        _test_independent_and_context_restore()
        _test_shared_atlas_pixel_padding()
        _test_independent_pack_mode()
        _test_shared_all_selected()
        _test_shared_partial_selection()
        _test_preview_does_not_change_seams()
        _test_finalize_failure_does_not_rollback()
        _test_atlas_pack_failure_cleanup()
        _test_commit_rollback_injection()
    finally:
        _clear_scene()
    print("[v1] 전체 통합 회귀 통과")


if __name__ == "__main__":
    main()
