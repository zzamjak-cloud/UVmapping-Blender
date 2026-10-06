"""매핑 파츠(면 그룹) 등록·편집·Isolate 연산자.

머리·몸통·팔·다리처럼 투영 매핑을 따로 진행할 면 묶음을 사용자가 면(쿼드)
단위로 등록한다. 파츠마다 FACE 도메인 BOOLEAN 속성 하나를 두므로 메시 데이터와
함께 ``.blend``에 저장되고, Modifier 평가 결과에도 속성이 따라간다.

면 속성이 파츠의 기준이다. 목록 항목(이름·종류)은 속성의 설명일 뿐이라, 속성이
없는 항목은 목록에서 숨기고(보관) 목록에 없는 파츠 속성은 다시 목록에 올린다.
Edit Mode undo는 면 속성만 되돌리고 Mesh의 목록은 되돌리지 않으므로, 이 규칙으로
"추가 후 undo"·"제거 후 undo"에서도 목록과 면이 어긋나지 않는다.

Isolate는 면 숨김(``hide``) 플래그만 바꾼다. 셰이더나 머티리얼은 건드리지 않으며,
숨김은 Edit Mode와 Paint 모드 뷰포트에서 보인다(Object Mode 표시·렌더에는 영향 없음).

3DPainter 애드온의 파츠 Isolate 기능(GPL-3.0-or-later)을 참고해 매핑 용도로 다시 썼다.
"""

from __future__ import annotations

from contextlib import contextmanager
import uuid

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup

from . import mapping_parts
from .mapping_parts import CUSTOM_KIND


# 파츠 면 속성 이름 접두사. 메시마다 번호를 1부터 매기면 Ctrl+J 결합 때 다른 파츠끼리
# 같은 이름으로 합쳐지므로 전역 고유 접미사를 붙인다.
ATTR_PREFIX = "uvmapping_part_"
# Isolate 직전의 면 숨김 상태. 점으로 시작하는 속성은 UI 목록에 보이지 않는다.
PREV_HIDE_ATTR = ".uvmapping_prev_hide"
# 속성이 사라진(제거되었거나 undo된) 항목을 이 개수까지만 보관한다.
_ARCHIVE_LIMIT = 32

_KIND_ITEMS = mapping_parts.kind_enum_items()
_PRESET_LABELS = {preset.label for preset in mapping_parts.BODY_PART_PRESETS}

# 코드가 종류를 정할 때는 사용자가 직접 고른 것으로 보지 않는다.
_assigning_kind = False


def _label_stem(name: str) -> str:
    """``머리.001``처럼 번호 접미사를 뗀 이름."""

    base = name.strip().rsplit(".", 1)
    return base[0] if len(base) == 2 and base[1].isdigit() else name.strip()


def _set_kind_automatically(part, kind: str) -> None:
    """종류 잠금을 걸지 않고 종류를 바꾼다."""

    global _assigning_kind
    _assigning_kind = True
    try:
        part.kind = kind
    finally:
        _assigning_kind = False


def _on_part_name_update(part, _context) -> None:
    """이름이 인체 단위 매칭 단어와 맞으면 종류를 따라 바꾼다(종류를 직접 고른 파츠는 제외)."""

    if part.kind_locked:
        return
    matched = mapping_parts.match_body_part(part.name)
    if matched != CUSTOM_KIND and matched != part.kind:
        _set_kind_automatically(part, matched)


def _on_part_kind_update(part, _context) -> None:
    """종류를 바꾸면 기본 이름을 쓰던 파츠는 새 종류의 이름으로 바꾼다."""

    if not _assigning_kind:
        # 목록에서 직접 고른 종류는 이후 이름을 바꿔도 유지한다.
        part.kind_locked = True
    preset = mapping_parts.preset_for_kind(part.kind)
    if preset is None:
        return
    stem = _label_stem(part.name)
    # 사용자가 직접 붙인 이름은 지키고, 비었거나 다른 프리셋의 기본 이름일 때만 바꾼다.
    if stem and (stem not in _PRESET_LABELS or stem == preset.label):
        return
    mesh = part.id_data
    others = [item.name for _index, item in live_parts(mesh) if item != part]
    part.name = mapping_parts.default_part_name(part.kind, others, len(others) + 1)


class UVMAPPING_PG_mapping_part(PropertyGroup):
    """매핑 파츠 하나. 면 소속은 ``attr`` 이름의 FACE 속성에 저장한다."""

    name: StringProperty(name="이름", default="파츠", update=_on_part_name_update)
    kind: EnumProperty(
        name="종류",
        description="인체 단위 프리셋. 생성 프롬프트와 처리 순서에 쓰입니다",
        items=_KIND_ITEMS,
        default=CUSTOM_KIND,
        update=_on_part_kind_update,
    )
    # 사용자가 목록에서 종류를 직접 골랐는지. 켜져 있으면 이름으로 종류를 바꾸지 않는다.
    kind_locked: BoolProperty(default=False, options={"HIDDEN"})
    attr: StringProperty(options={"HIDDEN"})


class UVMAPPING_PG_mesh_parts(PropertyGroup):
    """Mesh 데이터 하나에 등록된 매핑 파츠 목록."""

    parts: CollectionProperty(type=UVMAPPING_PG_mapping_part)
    active_index: IntProperty(default=0)
    # Isolate 중인 파츠 인덱스(-1이면 전체 표시)
    isolated: IntProperty(default=-1)


def active_mesh_object(context):
    """활성 객체가 Mesh면 돌려준다."""

    view_layer = getattr(context, "view_layer", None)
    obj = view_layer.objects.active if view_layer else None
    if obj is None or obj.type != "MESH":
        return None
    return obj


def mesh_parts(obj):
    """객체 Mesh의 파츠 목록(등록 전이면 ``None``)."""

    if obj is None or obj.type != "MESH":
        return None
    return getattr(obj.data, "uvmapping_parts", None)


def _is_part_attribute(attr) -> bool:
    return attr is not None and attr.domain == "FACE" and attr.data_type == "BOOLEAN"


def is_live(mesh, part) -> bool:
    """항목의 면 속성이 남아 있는지(속성이 없으면 보관 상태로 목록에서 숨긴다)."""

    return bool(part.attr) and _is_part_attribute(mesh.attributes.get(part.attr))


def live_parts(mesh) -> list[tuple[int, object]]:
    """면 속성이 있는 파츠만 (컬렉션 인덱스, 파츠)로 돌려준다."""

    return [
        (index, part)
        for index, part in enumerate(mesh.uvmapping_parts.parts)
        if is_live(mesh, part)
    ]


def sync_parts(mesh) -> None:
    """목록을 면 속성에 맞춘다.

    목록에 없는 파츠 속성(Join으로 들어온 속성, 목록 제거 뒤 남은 속성)은 사용자 지정
    파츠로 다시 올리고, 속성이 없는 보관 항목은 ``_ARCHIVE_LIMIT``개까지만 남긴다.
    """

    data = mesh.uvmapping_parts
    known = {part.attr for part in data.parts}
    for attr in list(mesh.attributes):
        if not attr.name.startswith(ATTR_PREFIX) or attr.name in known:
            continue
        if not _is_part_attribute(attr):
            continue
        names = [part.name for _index, part in live_parts(mesh)]
        part = data.parts.add()
        part.attr = attr.name
        part.name = mapping_parts.default_part_name(CUSTOM_KIND, names, len(names) + 1)

    archived = [index for index, part in enumerate(data.parts) if not is_live(mesh, part)]
    if len(archived) <= _ARCHIVE_LIMIT or data.isolated >= 0:
        return
    active_attr = data.parts[data.active_index].attr if 0 <= data.active_index < len(data.parts) else ""
    for index in reversed(archived[: len(archived) - _ARCHIVE_LIMIT]):
        data.parts.remove(index)
    data.active_index = next(
        (index for index, part in enumerate(data.parts) if part.attr == active_attr), 0
    )


def _active_live_part(mesh):
    data = mesh.uvmapping_parts
    if not 0 <= data.active_index < len(data.parts):
        return None
    part = data.parts[data.active_index]
    return part if is_live(mesh, part) else None


@contextmanager
def _object_mode_data(obj):
    """면 속성을 읽고 쓰는 동안만 Object Mode로 둔다.

    Edit Mode에서는 면 데이터가 BMesh에 있어 ``mesh.attributes`` 값이 최신이 아니고,
    써도 Edit Mode를 나갈 때 덮어써진다. 모드를 바꿔 편집 내용을 먼저 반영한다.
    """

    was_edit = obj.mode == "EDIT"
    if was_edit:
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        yield obj.data
    finally:
        if was_edit:
            bpy.ops.object.mode_set(mode="EDIT")


def part_face_mask(mesh, part) -> np.ndarray | None:
    """파츠에 속한 면 마스크. 속성이 없거나 형식이 다르면 ``None``."""

    attr = mesh.attributes.get(part.attr) if part.attr else None
    if not _is_part_attribute(attr):
        return None
    mask = np.zeros(len(mesh.polygons), dtype=bool)
    if len(attr.data) == len(mask):
        attr.data.foreach_get("value", mask)
    return mask


def part_face_masks(mesh) -> list[tuple[object, np.ndarray]]:
    """목록 순서대로 살아 있는 파츠의 (파츠, 면 마스크)."""

    return [(part, part_face_mask(mesh, part)) for _index, part in live_parts(mesh)]


def _write_face_mask(mesh, attr_name: str, mask: np.ndarray) -> None:
    attr = mesh.attributes.get(attr_name)
    if attr is not None and not _is_part_attribute(attr):
        # 같은 이름의 다른 형식 속성은 크기가 달라 쓸 수 없으므로 새로 만든다.
        mesh.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.attributes.new(attr_name, "BOOLEAN", "FACE")
    attr.data.foreach_set("value", mask)


def _new_attr_name(mesh) -> str:
    while True:
        name = f"{ATTR_PREFIX}{uuid.uuid4().hex[:12]}"
        if mesh.attributes.get(name) is None:
            return name


def _face_flags(mesh, name: str) -> np.ndarray:
    flags = np.zeros(len(mesh.polygons), dtype=bool)
    mesh.polygons.foreach_get(name, flags)
    return flags


def _release_from_other_parts(mesh, keep_attr: str, faces: np.ndarray) -> int:
    """``faces``를 다른 파츠에서 뺀다. 면 하나가 한 파츠에만 속하게 유지한다."""

    released = 0
    for part, mask in part_face_masks(mesh):
        if part.attr == keep_attr:
            continue
        overlap = mask & faces
        if overlap.any():
            released += int(overlap.sum())
            _write_face_mask(mesh, part.attr, mask & ~faces)
    return released


def coverage_report(mesh) -> dict:
    """파츠 등록 상태 검사: 미등록 면, 중복 등록 면, 빈 파츠, 중복 종류."""

    face_count = len(mesh.polygons)
    owners = np.zeros(face_count, dtype=np.int32)
    empty: list[str] = []
    kinds: list[str] = []
    for part, mask in part_face_masks(mesh):
        if not mask.any():
            empty.append(part.name)
        owners += mask
        kinds.append(part.kind)
    return {
        "faces": face_count,
        "unassigned": int((owners == 0).sum()),
        "overlapping": int((owners > 1).sum()),
        "empty_parts": tuple(empty),
        "duplicate_kinds": mapping_parts.duplicate_kinds(kinds),
    }


def _element_visibility(mesh, visible_faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """보이는 면에 속한 정점·변만 보이게 한다.

    변을 양 끝 정점으로 판단하면, 숨긴 면에만 속한 변이 보이는 정점 둘 사이에서
    드러나 Edit Mode 편집이 숨긴 면에 번질 수 있다.
    """

    loop_total = np.zeros(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", loop_total)
    loop_visible = np.repeat(visible_faces, loop_total)
    loop_vertices = np.zeros(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", loop_vertices)
    loop_edges = np.zeros(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("edge_index", loop_edges)
    vertex_visible = np.zeros(len(mesh.vertices), dtype=bool)
    vertex_visible[loop_vertices[loop_visible]] = True
    edge_visible = np.zeros(len(mesh.edges), dtype=bool)
    edge_visible[loop_edges[loop_visible]] = True
    return vertex_visible, edge_visible


def _apply_face_hide(mesh, hidden_faces: np.ndarray) -> None:
    """면 숨김을 정하고 정점·변 숨김을 맞춘다. 숨겨지는 요소의 선택은 푼다."""

    visible = ~hidden_faces
    vertex_visible, edge_visible = _element_visibility(mesh, visible)
    for collection, shown in (
        (mesh.polygons, visible),
        (mesh.edges, edge_visible),
        (mesh.vertices, vertex_visible),
    ):
        selected = np.zeros(len(collection), dtype=bool)
        collection.foreach_get("select", selected)
        collection.foreach_set("hide", ~shown)
        collection.foreach_set("select", selected & shown)
    mesh.update()


def _isolation_active(mesh, data) -> bool:
    """``data.isolated``가 실제 숨김 상태와 맞는지. undo로 숨김만 풀린 경우를 걸러낸다."""

    if not 0 <= data.isolated < len(data.parts):
        return False
    mask = part_face_mask(mesh, data.parts[data.isolated])
    return mask is not None and np.array_equal(_face_flags(mesh, "hide"), ~mask)


def _restore_hide(mesh) -> None:
    """Isolate 전 숨김 상태로 되돌리고 저장해 둔 속성을 지운다."""

    face_count = len(mesh.polygons)
    hidden = np.zeros(face_count, dtype=bool)
    saved = mesh.attributes.get(PREV_HIDE_ATTR)
    if _is_part_attribute(saved) and len(saved.data) == face_count:
        saved.data.foreach_get("value", hidden)
    if saved is not None:
        mesh.attributes.remove(saved)
    _apply_face_hide(mesh, hidden)


def isolate_part(obj, index: int) -> str | None:
    """``index`` 파츠만 보이게 한다. 이미 Isolate 중인 파츠면 전체 표시로 되돌린다.

    Returns:
        실패 사유 문자열 또는 ``None``
    """

    data = mesh_parts(obj)
    if data is None:
        return "등록된 파츠가 없습니다"
    with _object_mode_data(obj) as mesh:
        sync_parts(mesh)
        if not 0 <= index < len(data.parts) or not is_live(mesh, data.parts[index]):
            return "등록된 파츠가 없습니다"
        active = _isolation_active(mesh, data)
        if active and data.isolated == index:
            _restore_hide(mesh)
            data.isolated = -1
            return None
        mask = part_face_mask(mesh, data.parts[index])
        if not mask.any():
            return f"'{data.parts[index].name}' 파츠에 면이 없습니다"
        if not active:
            # 처음 Isolate할 때만 사용자가 숨겨 둔 면을 기억한다.
            _write_face_mask(mesh, PREV_HIDE_ATTR, _face_flags(mesh, "hide"))
        _apply_face_hide(mesh, ~mask)
    data.isolated = index
    data.active_index = index
    return None


def show_all_parts(obj) -> None:
    """Isolate를 풀고 Isolate 전 숨김 상태로 되돌린다."""

    data = mesh_parts(obj)
    if data is None or data.isolated < 0:
        return
    with _object_mode_data(obj) as mesh:
        if _isolation_active(mesh, data) or mesh.attributes.get(PREV_HIDE_ATTR) is not None:
            _restore_hide(mesh)
    data.isolated = -1


def plan_part_passes(objects) -> tuple[mapping_parts.PartPass, ...]:
    """대상 객체들의 등록 파츠를 파츠별 매핑 패스로 묶는다(면이 있는 파츠만).

    Object Mode에서 불러야 면 속성이 최신이다.
    """

    entries = []
    for obj in objects:
        if obj is None or obj.type != "MESH":
            continue
        mesh = obj.data
        for part, mask in part_face_masks(mesh):
            if mask.any():
                entries.append((obj.name, part.kind, part.name, part.attr))
    return mapping_parts.plan_part_passes(entries)


def count_part_passes(objects) -> int:
    """패널 표시용 패스 수. 면 마스크를 읽지 않고 살아 있는 파츠 종류만 센다."""

    keys = {
        mapping_parts.part_key(part.kind, part.name)
        for obj in objects
        if obj is not None and obj.type == "MESH"
        for _index, part in live_parts(obj.data)
    }
    return len(keys)


def members_signature(objects, members) -> str:
    """파츠 패스의 면 구성 지문. 생성 뒤 파츠 면이 바뀌었는지 베이크 때 비교한다.

    Args:
        objects: 대상 객체들
        members: 객체 이름 → 면 속성 이름 목록
    """

    import hashlib

    digest = hashlib.sha256()
    by_name = {obj.name: obj for obj in objects}
    for name in sorted(members):
        obj = by_name.get(name)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if obj is None:
            digest.update(b"missing\0")
            continue
        mesh = obj.data
        combined = np.zeros(len(mesh.polygons), dtype=bool)
        for attr_name in sorted(members[name]):
            attr = mesh.attributes.get(attr_name)
            if not _is_part_attribute(attr) or len(attr.data) != len(combined):
                digest.update(f"missing:{attr_name}\0".encode("utf-8"))
                continue
            values = np.zeros(len(combined), dtype=bool)
            attr.data.foreach_get("value", values)
            combined |= values
        digest.update(np.packbits(combined).tobytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _redraw(context) -> None:
    screen = getattr(context, "screen", None)
    for area in screen.areas if screen else ():
        if area.type in {"VIEW_3D", "PROPERTIES"}:
            area.tag_redraw()


def _has_active_part(context) -> bool:
    obj = active_mesh_object(context)
    return obj is not None and _active_live_part(obj.data) is not None


class UVMAPPING_OT_add_mapping_part(Operator):
    """선택한 면으로 매핑 파츠를 만든다 (Edit Mode 면 선택)"""

    bl_idname = "uvmapping.add_mapping_part"
    bl_label = "매핑 파츠 추가"
    # REGISTER를 빼 Redo 패널을 띄우지 않는다. 거기서 값을 고치면 연산자가 다시 실행되어
    # 파츠가 하나 더 생긴다. 이름과 종류는 생성 전 대화상자에서 받는다.
    bl_options = {"UNDO"}

    kind: EnumProperty(name="종류", items=_KIND_ITEMS, default=CUSTOM_KIND)
    name: StringProperty(name="이름", default="")
    exclusive: BoolProperty(
        name="다른 파츠에서 빼기",
        description="선택한 면이 이미 다른 파츠에 있으면 그 파츠에서 빼 한 파츠에만 속하게 합니다",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return active_mesh_object(context) is not None

    def invoke(self, context, _event):
        mesh = active_mesh_object(context).data
        # 아직 없는 인체 단위를 미리 골라 두어 Enter만 눌러도 차례대로 등록되게 한다.
        self.kind = mapping_parts.next_unregistered_kind(
            part.kind for _index, part in live_parts(mesh)
        )
        self.name = ""
        return context.window_manager.invoke_props_dialog(
            self, title="매핑 파츠 추가", confirm_text="추가"
        )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "kind")
        if self.kind == CUSTOM_KIND:
            row = layout.row()
            row.activate_init = True
            row.prop(self, "name")
            layout.label(text="이름이 인체 단위와 맞으면 종류를 자동으로 정합니다", icon="INFO")
        layout.prop(self, "exclusive")

    def execute(self, context):
        obj = active_mesh_object(context)
        with _object_mode_data(obj) as mesh:
            sync_parts(mesh)
            data = mesh.uvmapping_parts
            selected = _face_flags(mesh, "select")
            if not selected.any():
                self.report({"WARNING"}, "선택된 면이 없습니다. Edit Mode에서 면을 선택하세요")
                return {"CANCELLED"}
            kind = self.kind
            # 프리셋을 고른 경우 대화상자에 숨은 이전 입력값은 쓰지 않는다.
            name = self.name.strip() if kind == CUSTOM_KIND else ""
            if name:
                kind = mapping_parts.match_body_part(name)
            if not name:
                live_names = [part.name for _index, part in live_parts(mesh)]
                name = mapping_parts.default_part_name(kind, live_names, len(live_names) + 1)
            attr_name = _new_attr_name(mesh)
            released = _release_from_other_parts(mesh, attr_name, selected) if self.exclusive else 0
            _write_face_mask(mesh, attr_name, selected)
            part = data.parts.add()
            part.attr = attr_name
            part.name = name
            _set_kind_automatically(part, kind)
            data.active_index = len(data.parts) - 1
        _redraw(context)
        message = f"파츠 '{name}' 추가: 면 {int(selected.sum())}개"
        if released:
            message += f" (다른 파츠에서 {released}개 이동)"
        self.report({"INFO"}, message)
        return {"FINISHED"}


class UVMAPPING_OT_assign_mapping_part(Operator):
    """활성 파츠의 면을 현재 선택으로 바꾸거나, 선택을 더하거나 뺀다"""

    bl_idname = "uvmapping.assign_mapping_part"
    bl_label = "파츠 면 지정"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="방식",
        items=(
            ("REPLACE", "교체", "활성 파츠의 면을 선택으로 바꿉니다"),
            ("ADD", "추가", "선택한 면을 활성 파츠에 더합니다"),
            ("REMOVE", "제거", "선택한 면을 활성 파츠에서 뺍니다"),
        ),
        default="REPLACE",
    )
    exclusive: BoolProperty(
        name="다른 파츠에서 빼기",
        description="추가·교체한 면을 다른 파츠에서 빼 한 파츠에만 속하게 합니다",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return _has_active_part(context)

    def execute(self, context):
        obj = active_mesh_object(context)
        with _object_mode_data(obj) as mesh:
            sync_parts(mesh)
            part = _active_live_part(mesh)
            if part is None:
                self.report({"WARNING"}, "활성 파츠가 없습니다")
                return {"CANCELLED"}
            selected = _face_flags(mesh, "select")
            if not selected.any():
                self.report({"WARNING"}, "선택된 면이 없습니다")
                return {"CANCELLED"}
            current = part_face_mask(mesh, part)
            if self.mode == "REPLACE":
                result = selected
            elif self.mode == "ADD":
                result = current | selected
            else:
                result = current & ~selected
            if self.exclusive and self.mode != "REMOVE":
                _release_from_other_parts(mesh, part.attr, selected)
            _write_face_mask(mesh, part.attr, result)
            name = part.name
        _redraw(context)
        self.report({"INFO"}, f"파츠 '{name}': 면 {int(result.sum())}개")
        return {"FINISHED"}


class UVMAPPING_OT_select_mapping_part(Operator):
    """활성 파츠의 면을 Edit Mode에서 선택한다"""

    bl_idname = "uvmapping.select_mapping_part"
    bl_label = "파츠 면 선택"
    bl_options = {"REGISTER", "UNDO"}

    extend: BoolProperty(name="선택 유지", description="기존 선택에 더합니다", default=False)

    @classmethod
    def poll(cls, context):
        obj = active_mesh_object(context)
        return obj is not None and obj.mode == "EDIT" and _has_active_part(context)

    def execute(self, context):
        obj = active_mesh_object(context)
        # 정점 선택 모드는 정점에서 면 선택을 다시 계산해 이웃 면까지 선택되므로,
        # 면 단위인 파츠는 면 선택 모드로 보여 준다.
        context.tool_settings.mesh_select_mode = (False, False, True)
        with _object_mode_data(obj) as mesh:
            part = _active_live_part(mesh)
            mask = part_face_mask(mesh, part) if part is not None else None
            if mask is None or not mask.any():
                self.report({"WARNING"}, "이 파츠에는 면이 없습니다")
                return {"CANCELLED"}
            selection = mask & ~_face_flags(mesh, "hide")
            if self.extend:
                selection |= _face_flags(mesh, "select")
            # 정점·변 선택도 맞춰야 Edit Mode에서 면 선택이 그대로 보인다.
            vertex_selected, edge_selected = _element_visibility(mesh, selection)
            mesh.polygons.foreach_set("select", selection)
            mesh.edges.foreach_set("select", edge_selected)
            mesh.vertices.foreach_set("select", vertex_selected)
        _redraw(context)
        return {"FINISHED"}


class UVMAPPING_OT_remove_mapping_part(Operator):
    """활성 파츠를 지운다 (메시 면은 그대로)"""

    bl_idname = "uvmapping.remove_mapping_part"
    bl_label = "매핑 파츠 제거"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _has_active_part(context)

    def execute(self, context):
        obj = active_mesh_object(context)
        data = mesh_parts(obj)
        if data.isolated >= 0:
            show_all_parts(obj)
        with _object_mode_data(obj) as mesh:
            sync_parts(mesh)
            part = _active_live_part(mesh)
            if part is None:
                return {"CANCELLED"}
            # 면 속성만 지우고 항목은 보관한다. Edit Mode undo로 속성이 돌아오면
            # 이름·종류도 함께 목록에 되살아난다.
            mesh.attributes.remove(mesh.attributes[part.attr])
            live = [index for index, _part in live_parts(mesh)]
            removed = data.active_index
            data.active_index = next(
                (index for index in live if index > removed),
                live[-1] if live else 0,
            )
        _redraw(context)
        return {"FINISHED"}


class UVMAPPING_OT_isolate_mapping_part(Operator):
    """파츠 하나만 보이게 한다 (같은 파츠를 다시 누르면 전체 표시)"""

    bl_idname = "uvmapping.isolate_mapping_part"
    bl_label = "파츠 Isolate"
    # 자주 토글하는 표시 상태라 undo 단계를 만들지 않는다.
    bl_options = {"REGISTER"}

    index: IntProperty(name="인덱스", default=0, min=0)

    @classmethod
    def poll(cls, context):
        return mesh_parts(active_mesh_object(context)) is not None

    def execute(self, context):
        error = isolate_part(active_mesh_object(context), self.index)
        if error:
            self.report({"WARNING"}, error)
            return {"CANCELLED"}
        _redraw(context)
        return {"FINISHED"}


class UVMAPPING_OT_show_all_mapping_parts(Operator):
    """Isolate를 풀고 모든 파츠를 다시 보이게 한다"""

    bl_idname = "uvmapping.show_all_mapping_parts"
    bl_label = "전체 표시"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        data = mesh_parts(active_mesh_object(context))
        return data is not None and data.isolated >= 0

    def execute(self, context):
        show_all_parts(active_mesh_object(context))
        _redraw(context)
        return {"FINISHED"}


classes = (
    UVMAPPING_PG_mapping_part,
    UVMAPPING_PG_mesh_parts,
    UVMAPPING_OT_add_mapping_part,
    UVMAPPING_OT_assign_mapping_part,
    UVMAPPING_OT_select_mapping_part,
    UVMAPPING_OT_remove_mapping_part,
    UVMAPPING_OT_isolate_mapping_part,
    UVMAPPING_OT_show_all_mapping_parts,
)


__all__ = (
    "ATTR_PREFIX",
    "PREV_HIDE_ATTR",
    "active_mesh_object",
    "classes",
    "count_part_passes",
    "coverage_report",
    "is_live",
    "isolate_part",
    "live_parts",
    "members_signature",
    "mesh_parts",
    "part_face_mask",
    "part_face_masks",
    "plan_part_passes",
    "show_all_parts",
    "sync_parts",
)
