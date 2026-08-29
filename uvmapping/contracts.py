"""향후 텍스처 생성 단계에 전달할 안정적인 데이터 계약."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from math import isclose, isfinite, sqrt
from typing import Any, Sequence

from .quality import (
    AtlasQualityReport,
    UVQualityReport,
    _EPSILON,
    _FaceData,
    _extract_faces,
    _signed_uv_area,
    _triangle_area_3d,
    _triangulate,
    evaluate_atlas_quality,
)


Vec2 = tuple[float, float]


@dataclass(frozen=True, slots=True)
class TextureJob:
    """AI 텍스처 공급자가 소비할 수 있는 JSON 호환 작업 설명.

    ``resolution``, ``padding``, ``udim_tiles``는 1.1 호출자 호환 별칭이며
    실제 의미는 각각 target/requested 필드에 명시한다. packing 결과의 margin은
    별도 ``packing_margin_*`` 필드에 알려진 값만 기록한다.
    """

    schema_version: str
    object_name: str
    mesh_hash: str
    uv_layer_name: str
    face_to_island: dict[int, int]
    island_adjacency: tuple[tuple[int, int], ...]
    seam_edges: tuple[int, ...]
    uv_bounds: tuple[Vec2, Vec2]
    texel_density: dict[str, Any]
    quality: dict[str, Any]
    addon_version: str = "1.1.0"
    job_id: str = ""
    topology_hash: str = ""
    geometry_hash: str = ""
    uv_hash: str = ""
    settings_hash: str = ""
    overlap_status: str = "EXACT"
    resolution: tuple[int, int] = (2048, 2048)
    padding: int = 16
    udim_tiles: tuple[int, ...] = (1001,)
    object_transform: tuple[tuple[float, float, float, float], ...] = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    coordinate_space: str = "OBJECT"
    material_face_mapping: dict[int, int] = field(default_factory=dict)
    islands: tuple[dict[str, Any], ...] = ()
    guide_map_manifest: dict[str, Any] = field(default_factory=dict)
    target_resolution: tuple[int, int] = (2048, 2048)
    requested_padding: int = 16
    requested_udim_tiles: tuple[int, ...] = (1001,)
    packing_margin_method: str = "FRACTION"
    packing_margin_uv: float = 0.0078125
    pack_shared_atlas: bool = True
    atlas_id: str = ""
    atlas_hash: str = ""
    atlas_member_id: str = ""
    atlas_members: tuple[dict[str, Any], ...] = ()
    global_island_offset: int = 0
    atlas_overlap_status: str = "EXACT"
    atlas_overlap_count: int = 0
    atlas_valid: bool = False
    atlas_out_of_bounds_count: int = 0
    atlas_bounds: tuple[Vec2, Vec2] = ((0.0, 0.0), (0.0, 0.0))
    atlas_member_count: int = 0
    atlas_triangle_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """중첩 dataclass와 tuple을 JSON 인코더가 처리할 사전으로 바꾼다."""

        return asdict(self)


def _canonical_float(value: float) -> str:
    number = float(value)
    if not isfinite(number):
        raise ValueError("메시 해시에는 유한한 좌표만 사용할 수 있습니다.")
    if number == 0.0:
        number = 0.0
    return format(number, ".12g")


def _stable_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return _canonical_float(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = value
        if isinstance(value, (set, frozenset)):
            items = sorted(value, key=str)
        return [_canonical_value(item) for item in items]
    return str(value)


def _content_hashes(
    vertices: Sequence[tuple[float, float, float]],
    faces: Sequence[_FaceData],
    settings_payload: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    topology_payload = {
        "faces": [
            {"index": face.index, "vertices": list(face.vertices)} for face in faces
        ]
    }
    topology_hash = _stable_digest(topology_payload)
    geometry_payload = {
        "topology_hash": topology_hash,
        "vertices": [
            [_canonical_float(component) for component in vertex]
            for vertex in vertices
        ],
    }
    geometry_hash = _stable_digest(geometry_payload)
    uv_payload = {
        "topology_hash": topology_hash,
        "faces": [
            {
                "index": face.index,
                "uvs": [
                    [_canonical_float(component) for component in uv]
                    for uv in face.uvs
                ],
            }
            for face in faces
        ],
    }
    uv_hash = _stable_digest(uv_payload)
    settings_hash = _stable_digest(_canonical_value(settings_payload))
    mesh_hash = _stable_digest(
        {
            "geometry_hash": geometry_hash,
            "topology_hash": topology_hash,
            "uv_hash": uv_hash,
        }
    )
    return topology_hash, geometry_hash, uv_hash, settings_hash, mesh_hash


class _DisjointSet:
    def __init__(self, items: Sequence[int]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        lower, upper = sorted((first_root, second_root))
        self.parent[upper] = lower


def _edge_index_by_key(mesh: Any) -> dict[tuple[int, int], int]:
    result: dict[tuple[int, int], int] = {}
    for position, edge in enumerate(getattr(mesh, "edges", ())):
        raw_vertices = tuple(getattr(edge, "vertices", ()))
        if len(raw_vertices) != 2:
            continue
        first = int(
            raw_vertices[0]
            if isinstance(raw_vertices[0], int)
            else raw_vertices[0].index
        )
        second = int(
            raw_vertices[1]
            if isinstance(raw_vertices[1], int)
            else raw_vertices[1].index
        )
        key = (min(first, second), max(first, second))
        result[key] = int(getattr(edge, "index", position))
    return result


def _uv_equal(first: Vec2, second: Vec2) -> bool:
    return abs(first[0] - second[0]) <= 1.0e-9 and abs(first[1] - second[1]) <= 1.0e-9


def _island_topology(
    mesh: Any,
    faces: Sequence[_FaceData],
    seam_edges: Sequence[int],
) -> tuple[dict[int, int], tuple[tuple[int, int], ...]]:
    face_indices = tuple(face.index for face in faces)
    disjoint = _DisjointSet(face_indices)
    edge_indices = _edge_index_by_key(mesh)
    seams = {int(index) for index in seam_edges}
    edge_faces: dict[tuple[int, int], list[tuple[int, dict[int, Vec2]]]] = {}
    for face in faces:
        for offset, first in enumerate(face.vertices):
            second_offset = (offset + 1) % len(face.vertices)
            second = face.vertices[second_offset]
            key = (min(first, second), max(first, second))
            edge_faces.setdefault(key, []).append(
                (
                    face.index,
                    {
                        first: face.uvs[offset],
                        second: face.uvs[second_offset],
                    },
                )
            )

    for key, linked in edge_faces.items():
        if edge_indices.get(key) in seams:
            continue
        for first_position, (first_face, first_uvs) in enumerate(linked):
            for second_face, second_uvs in linked[first_position + 1 :]:
                if all(_uv_equal(first_uvs[vertex], second_uvs[vertex]) for vertex in key):
                    disjoint.union(first_face, second_face)

    roots = {face_index: disjoint.find(face_index) for face_index in face_indices}
    ordered_roots = sorted(set(roots.values()), key=lambda root: min(
        face for face, face_root in roots.items() if face_root == root
    ))
    island_by_root = {root: index for index, root in enumerate(ordered_roots)}
    face_to_island = {
        face_index: island_by_root[root]
        for face_index, root in sorted(roots.items())
    }
    adjacency: set[tuple[int, int]] = set()
    for linked in edge_faces.values():
        linked_islands = sorted({face_to_island[face] for face, _ in linked})
        for first_position, first in enumerate(linked_islands):
            for second in linked_islands[first_position + 1 :]:
                adjacency.add((first, second))
    return face_to_island, tuple(sorted(adjacency))


def _texel_density(
    faces: Sequence[_FaceData],
    face_to_island: dict[int, int],
) -> dict[str, Any]:
    triangles = _triangulate(faces)
    totals: dict[int, list[float]] = {}
    total_geometry = 0.0
    total_uv = 0.0
    for triangle in triangles:
        geometry_area = _triangle_area_3d(triangle.coordinates)
        uv_area = abs(_signed_uv_area(triangle.uvs))
        if geometry_area <= _EPSILON or uv_area <= _EPSILON:
            continue
        island = face_to_island[triangle.face_index]
        island_totals = totals.setdefault(island, [0.0, 0.0])
        island_totals[0] += geometry_area
        island_totals[1] += uv_area
        total_geometry += geometry_area
        total_uv += uv_area

    per_island = {
        str(island): sqrt(uv_area / geometry_area)
        for island, (geometry_area, uv_area) in sorted(totals.items())
        if geometry_area > _EPSILON
    }
    values = list(per_island.values())
    return {
        "global": sqrt(total_uv / total_geometry)
        if total_geometry > _EPSILON
        else 0.0,
        "minimum": min(values, default=0.0),
        "maximum": max(values, default=0.0),
        "per_island": per_island,
        "unit": "uv_per_object_unit",
    }


def _normalize_resolution(value: int | Sequence[int]) -> tuple[int, int]:
    if isinstance(value, int):
        result = (value, value)
    else:
        items = tuple(int(item) for item in value)
        if len(items) != 2:
            raise ValueError("resolution은 정수 또는 너비/높이 2개여야 합니다.")
        result = (items[0], items[1])
    if result[0] < 1 or result[1] < 1:
        raise ValueError("resolution 값은 1 이상이어야 합니다.")
    return result


def _normalize_transform(
    value: Sequence[Sequence[float]] | None,
) -> tuple[tuple[float, float, float, float], ...]:
    if value is None:
        return (
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    rows = tuple(tuple(float(component) for component in row) for row in value)
    if len(rows) != 4 or any(len(row) != 4 for row in rows):
        raise ValueError("object_transform은 4x4 행렬이어야 합니다.")
    return tuple((row[0], row[1], row[2], row[3]) for row in rows)


def _material_face_mapping(mesh: Any) -> dict[int, int]:
    return {
        int(getattr(polygon, "index", position)): int(
            getattr(polygon, "material_index", 0)
        )
        for position, polygon in enumerate(mesh.polygons)
    }


def _island_manifest(
    faces: Sequence[_FaceData],
    face_to_island: Mapping[int, int],
    adjacency: Sequence[tuple[int, int]],
) -> tuple[dict[str, Any], ...]:
    adjacent: dict[int, set[int]] = {}
    for first, second in adjacency:
        adjacent.setdefault(first, set()).add(second)
        adjacent.setdefault(second, set()).add(first)
    grouped: dict[int, list[_FaceData]] = {}
    for face in faces:
        grouped.setdefault(face_to_island[face.index], []).append(face)
    result: list[dict[str, Any]] = []
    for island_id, island_faces in sorted(grouped.items()):
        points = [uv for face in island_faces for uv in face.uvs]
        bounds = (
            (min(point[0] for point in points), min(point[1] for point in points)),
            (max(point[0] for point in points), max(point[1] for point in points)),
        )
        result.append(
            {
                "island_id": island_id,
                "faces": tuple(sorted(face.index for face in island_faces)),
                "bounds": bounds,
                "adjacent_islands": tuple(sorted(adjacent.get(island_id, set()))),
            }
        )
    return tuple(result)


def _default_guide_map_manifest(coordinate_space: str) -> dict[str, Any]:
    maps = {}
    for kind in ("normal", "position", "ambient_occlusion", "curvature", "material_id"):
        maps[kind] = {
            "status": "NOT_GENERATED",
            "uri": None,
            "coordinate_space": coordinate_space,
        }
    return {"schema_version": "1.0", "maps": maps}


def _atlas_descriptor(value: Any) -> dict[str, Any]:
    if isinstance(value, TextureJob):
        return {
            "member_id": value.atlas_member_id or value.object_name,
            "island_count": len(value.islands),
            "object_name": value.object_name,
            "uv_layer_name": value.uv_layer_name,
        }
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return {"member_id": value[0], "island_count": value[1]}
    raise TypeError(
        "Atlas member descriptor는 TextureJob, mapping 또는 "
        "(member_id, island_count)여야 합니다."
    )


def build_atlas_context(
    report: AtlasQualityReport,
    member_jobs_or_descriptors: Sequence[Any],
    shared: bool,
    atlas_id: str | None = None,
) -> dict[str, Any]:
    """Atlas 품질 결과와 member별 island 수를 전역 manifest로 결합한다."""

    descriptors = [_atlas_descriptor(value) for value in member_jobs_or_descriptors]
    normalized: dict[str, dict[str, Any]] = {}
    for descriptor in descriptors:
        member_id = str(descriptor.get("member_id", "")).strip()
        if not member_id:
            raise ValueError("Atlas member descriptor의 member_id가 비어 있습니다.")
        if member_id in normalized:
            raise ValueError("Atlas member descriptor의 member_id는 고유해야 합니다.")
        island_count = int(descriptor.get("island_count", 0))
        if island_count < 0:
            raise ValueError("Atlas member의 island_count는 0 이상이어야 합니다.")
        normalized[member_id] = {**descriptor, "member_id": member_id, "island_count": island_count}

    report_ids = set(report.member_uv_hashes)
    if set(normalized) != report_ids:
        raise ValueError("Atlas report와 member descriptor의 member_id가 일치해야 합니다.")
    offset = 0
    members: list[dict[str, Any]] = []
    for member_id in sorted(normalized):
        descriptor = normalized[member_id]
        island_count = descriptor["island_count"]
        member = {
            "member_id": member_id,
            "uv_hash": report.member_uv_hashes[member_id],
            "bounds": report.member_bounds[member_id],
            "island_count": island_count,
            "global_island_offset": offset,
            "global_island_ids": tuple(range(offset, offset + island_count)),
        }
        for optional in ("object_name", "uv_layer_name", "mesh_name"):
            if optional in descriptor:
                member[optional] = descriptor[optional]
        if "object_names" in descriptor:
            raw_object_names = descriptor["object_names"]
            if isinstance(raw_object_names, str):
                raw_object_names = (raw_object_names,)
            if not isinstance(raw_object_names, (tuple, list, set, frozenset)):
                raise ValueError("Atlas member의 object_names는 문자열 목록이어야 합니다.")
            member["object_names"] = tuple(
                sorted(
                    {
                        str(object_name).strip()
                        for object_name in raw_object_names
                        if str(object_name).strip()
                    }
                )
            )
        members.append(member)
        offset += island_count
    resolved_atlas_id = str(atlas_id).strip() if atlas_id is not None else ""
    if not resolved_atlas_id:
        resolved_atlas_id = f"atlas-{report.atlas_hash[:16]}"
    return {
        "schema_version": "1.0",
        "atlas_id": resolved_atlas_id,
        "atlas_hash": report.atlas_hash,
        "shared": bool(shared),
        "valid": report.valid,
        "bounds": report.bounds,
        "member_count": report.member_count,
        "triangle_count": report.triangle_count,
        "out_of_bounds_count": report.out_of_bounds_count,
        "overlap_status": report.overlap_status,
        "overlap_count": report.overlap_pairs,
        "members": tuple(members),
        "global_island_count": offset,
    }


def _validated_atlas_bounds(value: Any, field_name: str) -> tuple[Vec2, Vec2]:
    try:
        lower, upper = value
        result = (
            (float(lower[0]), float(lower[1])),
            (float(upper[0]), float(upper[1])),
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 형식이 올바르지 않습니다.") from exc
    return result


def _validate_atlas_context(
    context: Mapping[str, Any],
    current_member_id: str,
    current_uv_hash: str,
    current_island_count: int,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], dict[str, Any]]:
    normalized = dict(context)
    if normalized.get("schema_version") != "1.0":
        raise ValueError("지원하지 않는 Atlas context schema_version입니다.")
    atlas_id = str(normalized.get("atlas_id", "")).strip()
    atlas_hash = str(normalized.get("atlas_hash", ""))
    if not atlas_id or len(atlas_hash) != 64:
        raise ValueError("Atlas context의 id 또는 hash가 올바르지 않습니다.")
    if not isinstance(normalized.get("valid"), bool):
        raise ValueError("Atlas context의 valid 값은 bool이어야 합니다.")
    if not isinstance(normalized.get("shared"), bool):
        raise ValueError("Atlas context의 shared 값은 bool이어야 합니다.")
    atlas_bounds = _validated_atlas_bounds(normalized.get("bounds"), "Atlas bounds")
    overlap_status = normalized.get("overlap_status")
    if overlap_status not in {"EXACT", "BUDGET_EXCEEDED"}:
        raise ValueError("Atlas context의 overlap_status가 올바르지 않습니다.")
    for name in (
        "out_of_bounds_count",
        "overlap_count",
        "member_count",
        "triangle_count",
        "global_island_count",
    ):
        value = normalized.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"Atlas context의 {name} 값이 올바르지 않습니다.")

    raw_members = normalized.get("members")
    if not isinstance(raw_members, (tuple, list)):
        raise ValueError("Atlas context의 members 형식이 올바르지 않습니다.")
    members: list[dict[str, Any]] = []
    member_bounds: list[tuple[Vec2, Vec2]] = []
    member_ids: set[str] = set()
    global_ids: set[int] = set()
    current_member = None
    for raw_member in raw_members:
        if not isinstance(raw_member, Mapping):
            raise ValueError("Atlas member manifest는 mapping이어야 합니다.")
        member = dict(raw_member)
        member_id = str(member.get("member_id", "")).strip()
        uv_hash = str(member.get("uv_hash", ""))
        if not member_id or member_id in member_ids or len(uv_hash) != 64:
            raise ValueError("Atlas member id 또는 uv_hash가 올바르지 않습니다.")
        member_ids.add(member_id)
        normalized_member_bounds = _validated_atlas_bounds(
            member.get("bounds"), "Atlas member bounds"
        )
        member_bounds.append(normalized_member_bounds)
        island_count = member.get("island_count")
        offset = member.get("global_island_offset")
        ids_value = member.get("global_island_ids")
        if (
            not isinstance(island_count, int)
            or isinstance(island_count, bool)
            or island_count < 0
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(ids_value, (tuple, list))
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in ids_value
            )
        ):
            raise ValueError("Atlas member island manifest가 올바르지 않습니다.")
        ids = tuple(ids_value)
        expected_ids = tuple(range(offset, offset + island_count))
        if ids != expected_ids or any(value in global_ids for value in ids):
            raise ValueError("Atlas global island ID가 중복되거나 offset과 다릅니다.")
        global_ids.update(ids)
        member["member_id"] = member_id
        member["uv_hash"] = uv_hash
        member["bounds"] = normalized_member_bounds
        member["global_island_ids"] = ids
        members.append(member)
        if member_id == current_member_id:
            current_member = member

    if len(members) != normalized["member_count"]:
        raise ValueError("Atlas member_count가 manifest와 일치하지 않습니다.")
    expected_atlas_hash = _stable_digest(
        {
            "members": [
                {"member_id": member["member_id"], "uv_hash": member["uv_hash"]}
                for member in sorted(members, key=lambda item: item["member_id"])
            ]
        }
    )
    if atlas_hash != expected_atlas_hash:
        raise ValueError("Atlas context의 atlas_hash가 member manifest와 다릅니다.")
    if member_bounds:
        expected_bounds = (
            (
                min(bounds[0][0] for bounds in member_bounds),
                min(bounds[0][1] for bounds in member_bounds),
            ),
            (
                max(bounds[1][0] for bounds in member_bounds),
                max(bounds[1][1] for bounds in member_bounds),
            ),
        )
        if atlas_bounds != expected_bounds:
            raise ValueError("Atlas bounds가 member bounds를 포함한 범위와 다릅니다.")
    computed_out_of_bounds = sum(
        lower[0] < -_EPSILON
        or lower[1] < -_EPSILON
        or upper[0] > 1.0 + _EPSILON
        or upper[1] > 1.0 + _EPSILON
        for lower, upper in member_bounds
    )
    if normalized["out_of_bounds_count"] != computed_out_of_bounds:
        raise ValueError("Atlas out_of_bounds_count가 member bounds와 다릅니다.")
    if sorted(global_ids) != list(range(normalized["global_island_count"])):
        raise ValueError("Atlas global island ID가 연속 범위를 이루지 않습니다.")
    expected_valid = bool(members) and normalized["triangle_count"] > 0 and not (
        normalized["overlap_count"]
        or normalized["overlap_status"] != "EXACT"
        or normalized["out_of_bounds_count"]
    )
    if normalized["valid"] != expected_valid:
        raise ValueError("Atlas context의 valid 값이 품질 지표와 일치하지 않습니다.")
    if current_member is None:
        raise ValueError("atlas_context에서 현재 atlas_member_id를 찾을 수 없습니다.")
    if current_member["uv_hash"] != current_uv_hash:
        raise ValueError("atlas_context의 member uv_hash가 현재 UV와 일치하지 않습니다.")
    if current_member["island_count"] != current_island_count:
        raise ValueError("atlas_context의 island_count가 현재 메시와 일치하지 않습니다.")
    normalized["atlas_id"] = atlas_id
    normalized["atlas_hash"] = atlas_hash
    normalized["members"] = tuple(members)
    return normalized, tuple(members), current_member


def build_texture_job(
    mesh: Any,
    object_name: str,
    uv_layer_name: str | None,
    quality_report: UVQualityReport,
    seam_edges: Sequence[int],
    *,
    addon_version: str = "1.1.0",
    resolution: int | Sequence[int] = (2048, 2048),
    padding: int = 16,
    udim_tiles: Sequence[int] = (1001,),
    object_transform: Sequence[Sequence[float]] | None = None,
    coordinate_space: str = "OBJECT",
    settings: Mapping[str, Any] | None = None,
    guide_map_manifest: Mapping[str, Any] | None = None,
    packing_margin_method: str | None = "FRACTION",
    packing_margin_uv: float | None = None,
    pack_shared_atlas: bool | None = None,
    atlas_context: Mapping[str, Any] | None = None,
    atlas_member_id: str | None = None,
) -> TextureJob:
    """검증된 UV 메시에서 결정론적인 텍스처 작업 계약을 만든다."""

    resolved_layer_name, faces, vertices = _extract_faces(mesh, uv_layer_name)
    normalized_seams = tuple(sorted({int(index) for index in seam_edges}))
    normalized_resolution = _normalize_resolution(resolution)
    normalized_padding = int(padding)
    if normalized_padding < 0:
        raise ValueError("padding 값은 0 이상이어야 합니다.")
    normalized_udims = tuple(sorted({int(tile) for tile in udim_tiles}))
    if not normalized_udims or any(tile < 1001 for tile in normalized_udims):
        raise ValueError("udim_tiles에는 1001 이상의 타일이 하나 이상 필요합니다.")
    normalized_transform = _normalize_transform(object_transform)
    normalized_space = str(coordinate_space).strip().upper()
    if not normalized_space:
        raise ValueError("coordinate_space 값은 비어 있을 수 없습니다.")
    normalized_margin_method = (
        str(packing_margin_method).strip().upper()
        if packing_margin_method is not None
        else "FRACTION"
    )
    if normalized_margin_method == "":
        raise ValueError("packing_margin_method 값은 비어 있을 수 없습니다.")
    derived_margin_uv = normalized_padding / min(normalized_resolution)
    normalized_margin_uv = (
        float(packing_margin_uv)
        if packing_margin_uv is not None
        else derived_margin_uv
    )
    if normalized_margin_uv < 0.0:
        raise ValueError("packing_margin_uv 값은 0 이상이어야 합니다.")
    if normalized_margin_method == "FRACTION" and not isclose(
        normalized_margin_uv,
        derived_margin_uv,
        abs_tol=1.0e-12,
    ):
        raise ValueError(
            "packing_margin_uv는 padding / resolution 값과 일치해야 합니다."
        )
    normalized_settings = dict(settings or {})
    requested_shared_atlas = bool(
        normalized_settings.get("pack_shared_atlas", True)
        if pack_shared_atlas is None
        else pack_shared_atlas
    )
    face_to_island, adjacency = _island_topology(
        mesh,
        faces,
        normalized_seams,
    )
    islands = _island_manifest(faces, face_to_island, adjacency)
    current_uv_hash = _content_hashes(vertices, faces, {})[2]
    normalized_member_id = str(atlas_member_id or object_name).strip()
    if not normalized_member_id:
        raise ValueError("atlas_member_id는 비어 있을 수 없습니다.")
    if atlas_context is None:
        atlas_report = evaluate_atlas_quality(
            [(normalized_member_id, mesh, resolved_layer_name)]
        )
        resolved_atlas_context = build_atlas_context(
            atlas_report,
            [
                {
                    "member_id": normalized_member_id,
                    "island_count": len(islands),
                    "object_name": str(object_name),
                    "uv_layer_name": resolved_layer_name,
                }
            ],
            shared=requested_shared_atlas,
        )
    else:
        resolved_atlas_context = dict(atlas_context)
    resolved_atlas_context, atlas_members, atlas_member = _validate_atlas_context(
        resolved_atlas_context,
        normalized_member_id,
        current_uv_hash,
        len(islands),
    )
    global_island_offset = int(atlas_member.get("global_island_offset", 0))
    islands = tuple(
        {
            **island,
            "global_island_id": global_island_offset + int(island["island_id"]),
        }
        for island in islands
    )
    normalized_shared_atlas = bool(
        resolved_atlas_context.get("shared", requested_shared_atlas)
    )
    settings_payload = {
        "target_resolution": normalized_resolution,
        "requested_padding": normalized_padding,
        "requested_udim_tiles": normalized_udims,
        "packing_margin_method": normalized_margin_method,
        "packing_margin_uv": normalized_margin_uv,
        "pack_shared_atlas": normalized_shared_atlas,
        "object_transform": normalized_transform,
        "coordinate_space": normalized_space,
        "settings": normalized_settings,
    }
    topology_hash, geometry_hash, uv_hash, settings_hash, mesh_hash = (
        _content_hashes(vertices, faces, settings_payload)
    )
    if uv_hash != current_uv_hash:
        raise RuntimeError("TextureJob UV hash 계산이 비결정적으로 변경되었습니다.")
    job_id = _stable_digest(
        {
            "atlas_hash": resolved_atlas_context.get("atlas_hash", ""),
            "atlas_member_id": normalized_member_id,
            "mesh_hash": mesh_hash,
            "object_name": str(object_name),
            "settings_hash": settings_hash,
        }
    )
    guide_manifest = (
        _canonical_value(guide_map_manifest)
        if guide_map_manifest is not None
        else _default_guide_map_manifest(normalized_space)
    )
    return TextureJob(
        schema_version="1.4",
        object_name=str(object_name),
        mesh_hash=mesh_hash,
        uv_layer_name=resolved_layer_name,
        face_to_island=face_to_island,
        island_adjacency=adjacency,
        seam_edges=normalized_seams,
        uv_bounds=quality_report.uv_bounds,
        texel_density=_texel_density(faces, face_to_island),
        quality=quality_report.to_dict(),
        addon_version=str(addon_version),
        job_id=job_id,
        topology_hash=topology_hash,
        geometry_hash=geometry_hash,
        uv_hash=uv_hash,
        settings_hash=settings_hash,
        overlap_status=quality_report.overlap_status,
        resolution=normalized_resolution,
        padding=normalized_padding,
        udim_tiles=normalized_udims,
        object_transform=normalized_transform,
        coordinate_space=normalized_space,
        material_face_mapping=_material_face_mapping(mesh),
        islands=islands,
        guide_map_manifest=guide_manifest,
        target_resolution=normalized_resolution,
        requested_padding=normalized_padding,
        requested_udim_tiles=normalized_udims,
        packing_margin_method=normalized_margin_method,
        packing_margin_uv=normalized_margin_uv,
        pack_shared_atlas=normalized_shared_atlas,
        atlas_id=str(resolved_atlas_context.get("atlas_id", "")),
        atlas_hash=str(resolved_atlas_context.get("atlas_hash", "")),
        atlas_member_id=normalized_member_id,
        atlas_members=atlas_members,
        global_island_offset=global_island_offset,
        atlas_overlap_status=str(
            resolved_atlas_context.get("overlap_status", "EXACT")
        ),
        atlas_overlap_count=int(resolved_atlas_context.get("overlap_count", 0)),
        atlas_valid=bool(resolved_atlas_context["valid"]),
        atlas_out_of_bounds_count=int(
            resolved_atlas_context["out_of_bounds_count"]
        ),
        atlas_bounds=_validated_atlas_bounds(
            resolved_atlas_context["bounds"], "Atlas bounds"
        ),
        atlas_member_count=int(resolved_atlas_context["member_count"]),
        atlas_triangle_count=int(resolved_atlas_context["triangle_count"]),
    )


__all__ = ("TextureJob", "build_atlas_context", "build_texture_job")
