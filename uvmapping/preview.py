"""Blender 데이터를 수정하지 않는 3D Viewport Seam 미리보기 오버레이."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import bpy
from bpy.app.handlers import persistent
import gpu
from gpu_extras.batch import batch_for_shader


PREVIEW_COLOR = (1.0, 0.22, 0.04, 0.95)
PREVIEW_LINE_WIDTH = 3.0


@dataclass(frozen=True, slots=True)
class _PreviewTarget:
    """RNA 참조 대신 다시 찾을 수 있는 세션 식별자만 보관합니다."""

    object_uid: int
    mesh_uid: int
    object_name: str


_targets: dict[int, _PreviewTarget] = {}
_mesh_edges: dict[int, tuple[int, ...]] = {}
_batch_cache: dict[int, Any | None] = {}
_draw_handle = None
_shader = None
_registered = False


def _session_uid(id_block) -> int:
    uid = getattr(id_block, "session_uid", 0)
    if not isinstance(uid, int) or uid <= 0:
        raise RuntimeError("Blender 세션 식별자를 확인할 수 없습니다.")
    return uid


def _get_shader():
    global _shader

    if _shader is None:
        _shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _shader


def mesh_edge_indices(mesh_or_uid) -> tuple[int, ...]:
    """Mesh에 등록된 런타임 Seam 후보 Edge 인덱스를 반환합니다."""

    mesh_uid = (
        mesh_or_uid
        if isinstance(mesh_or_uid, int)
        else getattr(mesh_or_uid, "session_uid", 0)
    )
    return _mesh_edges.get(mesh_uid, ())


def _build_batch(mesh):
    """Mesh geometry가 바뀔 때만 런타임 Edge 선분 배치를 다시 만듭니다."""

    edge_indices = tuple(
        index
        for index in mesh_edge_indices(mesh)
        if 0 <= index < len(mesh.edges)
    )
    if not edge_indices:
        return None

    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    for vertex in mesh.vertices:
        for axis in range(3):
            coordinate = float(vertex.co[axis])
            minimum[axis] = min(minimum[axis], coordinate)
            maximum[axis] = max(maximum[axis], coordinate)
    extent = max(
        (maximum[axis] - minimum[axis] for axis in range(3)),
        default=1.0,
    )
    surface_offset = max(extent, 1.0) * 1.0e-5
    coordinates = []
    for edge_index in edge_indices:
        edge = mesh.edges[edge_index]
        for vertex_index in edge.vertices:
            vertex = mesh.vertices[vertex_index]
            coordinates.append(tuple(vertex.co + vertex.normal * surface_offset))
    if not coordinates:
        return None
    return batch_for_shader(_get_shader(), "LINES", {"pos": coordinates})


def _batch_for_mesh(mesh):
    uid = _session_uid(mesh)
    if uid not in _batch_cache:
        _batch_cache[uid] = _build_batch(mesh)
    return _batch_cache[uid]


def _object_is_in_view_layer(context, obj) -> bool:
    try:
        return (
            context.view_layer.objects.get(obj.name) == obj
            and obj.visible_get(view_layer=context.view_layer)
        )
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def _resolve_object_fast(target: _PreviewTarget):
    """일반 draw 경로에서는 이름 해시 조회와 세션 ID 확인만 수행합니다."""

    obj = bpy.data.objects.get(target.object_name)
    if obj is not None and getattr(obj, "session_uid", 0) == target.object_uid:
        return obj
    return None


def _resolve_object_with_rename_fallback(target: _PreviewTarget):
    """Depsgraph/Undo 경계에서만 이름 변경 가능성을 전체 검색으로 복구합니다."""

    obj = _resolve_object_fast(target)
    if obj is not None:
        return obj
    for candidate in tuple(bpy.data.objects):
        if getattr(candidate, "session_uid", 0) != target.object_uid:
            continue
        _targets[target.object_uid] = _PreviewTarget(
            target.object_uid,
            target.mesh_uid,
            candidate.name_full,
        )
        return candidate
    return None


def _draw_callback_impl() -> None:
    """POST_VIEW에서 등록된 객체의 로컬 선분을 현재 변환으로 그립니다."""

    context = bpy.context
    area = getattr(context, "area", None)
    if area is None or area.type != "VIEW_3D" or not _targets:
        return

    resolved = []
    for target in tuple(_targets.values()):
        obj = _resolve_object_fast(target)
        if (
            obj is None
            or obj.type != "MESH"
            or obj.data is None
            or getattr(obj.data, "session_uid", 0) != target.mesh_uid
            or not _object_is_in_view_layer(context, obj)
        ):
            continue
        batch = _batch_for_mesh(obj.data)
        if batch is not None:
            resolved.append((obj, batch))
    if not resolved:
        return

    previous_blend = gpu.state.blend_get()
    previous_depth_mask = gpu.state.depth_mask_get()
    previous_depth_test = gpu.state.depth_test_get()
    previous_line_width = gpu.state.line_width_get()
    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_mask_set(False)
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.line_width_set(PREVIEW_LINE_WIDTH)
        shader = _get_shader()
        shader.bind()
        shader.uniform_float("color", PREVIEW_COLOR)
        for obj, batch in resolved:
            gpu.matrix.push()
            try:
                gpu.matrix.multiply_matrix(obj.matrix_world)
                batch.draw(shader)
            finally:
                gpu.matrix.pop()
    finally:
        gpu.state.line_width_set(previous_line_width)
        gpu.state.depth_test_set(previous_depth_test)
        gpu.state.depth_mask_set(previous_depth_mask)
        gpu.state.blend_set(previous_blend)


def _draw_callback() -> None:
    """데이터 변경과 GPU 컨텍스트 전환 중에도 handler 예외를 밖으로 내보내지 않습니다."""

    try:
        _draw_callback_impl()
    except (AttributeError, ReferenceError, RuntimeError, SystemError, ValueError):
        _batch_cache.clear()


def tag_redraw() -> None:
    """열려 있는 모든 3D Viewport에 안전하게 다시 그리기를 요청합니다."""

    try:
        windows = tuple(bpy.context.window_manager.windows)
    except (AttributeError, ReferenceError, RuntimeError):
        return
    for window in windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _ensure_draw_handler() -> None:
    global _draw_handle

    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_callback,
            (),
            "WINDOW",
            "POST_VIEW",
        )


def _remove_draw_handler() -> None:
    global _draw_handle

    handle = _draw_handle
    _draw_handle = None
    if handle is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
    except (ReferenceError, RuntimeError, ValueError):
        pass


def has_mesh_targets(mesh_or_uid) -> bool:
    """해당 Mesh 세션 ID를 사용하는 미리보기 대상이 남아 있는지 반환합니다."""

    mesh_uid = (
        mesh_or_uid
        if isinstance(mesh_or_uid, int)
        else getattr(mesh_or_uid, "session_uid", 0)
    )
    return any(target.mesh_uid == mesh_uid for target in _targets.values())


def _release_mesh_if_unused(mesh_uid: int) -> None:
    if not has_mesh_targets(mesh_uid):
        _mesh_edges.pop(mesh_uid, None)
        _batch_cache.pop(mesh_uid, None)


def activate_targets(objects, mesh_edge_items) -> int:
    """선택 객체와 Mesh별 후보 Edge를 런타임 registry에 누적합니다."""

    register()
    edges_by_mesh_uid = {}
    for mesh, edge_indices in tuple(mesh_edge_items):
        mesh_uid = _session_uid(mesh)
        edges_by_mesh_uid[mesh_uid] = tuple(
            sorted(
                {
                    int(index)
                    for index in edge_indices
                    if 0 <= int(index) < len(mesh.edges)
                }
            )
        )

    added = 0
    replaced_mesh_uids = set()
    for obj in tuple(objects):
        if obj is None or obj.type != "MESH" or obj.data is None:
            continue
        object_uid = _session_uid(obj)
        mesh_uid = _session_uid(obj.data)
        edge_indices = edges_by_mesh_uid.get(mesh_uid)
        if edge_indices is None:
            continue
        previous = _targets.get(object_uid)
        if previous is not None and previous.mesh_uid != mesh_uid:
            replaced_mesh_uids.add(previous.mesh_uid)
        target = _PreviewTarget(object_uid, mesh_uid, obj.name_full)
        if previous != target:
            added += 1
        _targets[object_uid] = target
        if _mesh_edges.get(mesh_uid) != edge_indices:
            _mesh_edges[mesh_uid] = edge_indices
            _batch_cache.pop(mesh_uid, None)
    for mesh_uid in replaced_mesh_uids:
        _release_mesh_if_unused(mesh_uid)
    if _targets:
        _ensure_draw_handler()
    tag_redraw()
    return added


def deactivate_targets(objects) -> int:
    """전달된 객체의 오버레이만 해제하고 공유 Mesh refcount를 반영합니다."""

    removed = 0
    mesh_uids = set()
    for obj in tuple(objects):
        uid = getattr(obj, "session_uid", 0)
        target = _targets.pop(uid, None)
        if target is not None:
            removed += 1
            mesh_uids.add(target.mesh_uid)
    for mesh_uid in mesh_uids:
        _release_mesh_if_unused(mesh_uid)
    if not _targets:
        _remove_draw_handler()
    tag_redraw()
    return removed


def deactivate_all() -> int:
    """모든 런타임 대상과 Edge, batch를 비우고 draw handler를 제거합니다."""

    count = len(_targets)
    _targets.clear()
    _mesh_edges.clear()
    _batch_cache.clear()
    _remove_draw_handler()
    tag_redraw()
    return count


def prune_stale_targets() -> int:
    """Undo, 데이터 교체, 삭제로 identity가 무효가 된 대상만 정리합니다."""

    stale = []
    for uid, target in tuple(_targets.items()):
        obj = _resolve_object_with_rename_fallback(target)
        if (
            obj is None
            or obj.type != "MESH"
            or obj.data is None
            or getattr(obj.data, "session_uid", 0) != target.mesh_uid
        ):
            stale.append(uid)
    mesh_uids = set()
    for uid in stale:
        mesh_uids.add(_targets.pop(uid).mesh_uid)
    for mesh_uid in mesh_uids:
        _release_mesh_if_unused(mesh_uid)
    if not _targets:
        _remove_draw_handler()
    return len(stale)


def is_active() -> bool:
    return bool(_targets)


def target_count() -> int:
    return len(_targets)


def target_object_uids() -> tuple[int, ...]:
    return tuple(sorted(_targets))


def draw_handler_active() -> bool:
    return _draw_handle is not None


def batch_cache_size() -> int:
    return len(_batch_cache)


@persistent
def _depsgraph_update(_scene, depsgraph) -> None:
    if not _targets:
        return
    updated_mesh_uids = set()
    for update in depsgraph.updates:
        if not getattr(update, "is_updated_geometry", False):
            continue
        id_block = update.id
        if isinstance(id_block, bpy.types.Mesh):
            updated_mesh_uids.add(getattr(id_block, "session_uid", 0))
        elif isinstance(id_block, bpy.types.Object):
            mesh = getattr(id_block, "data", None)
            if isinstance(mesh, bpy.types.Mesh):
                updated_mesh_uids.add(getattr(mesh, "session_uid", 0))
    for uid in updated_mesh_uids:
        _batch_cache.pop(uid, None)
    prune_stale_targets()
    if updated_mesh_uids:
        tag_redraw()


@persistent
def _history_update(*_args) -> None:
    _batch_cache.clear()
    prune_stale_targets()
    tag_redraw()


@persistent
def _load_update(*_args) -> None:
    deactivate_all()


_HANDLERS = (
    (bpy.app.handlers.depsgraph_update_post, _depsgraph_update),
    (bpy.app.handlers.undo_post, _history_update),
    (bpy.app.handlers.redo_post, _history_update),
    (bpy.app.handlers.load_post, _load_update),
)


def register() -> None:
    """Undo/Depsgraph 수명 주기 handler를 중복 없이 등록합니다."""

    global _registered

    for handlers, callback in _HANDLERS:
        if callback not in handlers:
            handlers.append(callback)
    _registered = True


def unregister() -> None:
    """오버레이와 수명 주기 handler를 여러 번 호출해도 안전하게 해제합니다."""

    global _registered, _shader

    deactivate_all()
    for handlers, callback in _HANDLERS:
        while callback in handlers:
            handlers.remove(callback)
    _shader = None
    _registered = False


def is_registered() -> bool:
    return _registered


__all__ = (
    "activate_targets",
    "batch_cache_size",
    "deactivate_all",
    "deactivate_targets",
    "draw_handler_active",
    "has_mesh_targets",
    "is_active",
    "is_registered",
    "mesh_edge_indices",
    "prune_stale_targets",
    "register",
    "tag_redraw",
    "target_count",
    "target_object_uids",
    "unregister",
)
