"""향후 텍스처 생성 단계에 전달할 안정적인 데이터 계약."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Any, Sequence

from .quality import (
    UVQualityReport,
    _EPSILON,
    _FaceData,
    _extract_faces,
    _signed_uv_area,
    _triangle_area_3d,
    _triangulate,
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
    addon_version: str = "1.0.0"
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
    packing_margin_method: str | None = None
    packing_margin_uv: float | None = None

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


def build_texture_job(
    mesh: Any,
    object_name: str,
    uv_layer_name: str | None,
    quality_report: UVQualityReport,
    seam_edges: Sequence[int],
    *,
    addon_version: str = "1.0.0",
    resolution: int | Sequence[int] = (2048, 2048),
    padding: int = 16,
    udim_tiles: Sequence[int] = (1001,),
    object_transform: Sequence[Sequence[float]] | None = None,
    coordinate_space: str = "OBJECT",
    settings: Mapping[str, Any] | None = None,
    guide_map_manifest: Mapping[str, Any] | None = None,
    packing_margin_method: str | None = None,
    packing_margin_uv: float | None = None,
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
        else None
    )
    if normalized_margin_method == "":
        raise ValueError("packing_margin_method 값은 비어 있을 수 없습니다.")
    normalized_margin_uv = (
        float(packing_margin_uv) if packing_margin_uv is not None else None
    )
    if normalized_margin_uv is not None and normalized_margin_uv < 0.0:
        raise ValueError("packing_margin_uv 값은 0 이상이어야 합니다.")
    settings_payload = {
        "target_resolution": normalized_resolution,
        "requested_padding": normalized_padding,
        "requested_udim_tiles": normalized_udims,
        "packing_margin_method": normalized_margin_method,
        "packing_margin_uv": normalized_margin_uv,
        "object_transform": normalized_transform,
        "coordinate_space": normalized_space,
        "settings": dict(settings or {}),
    }
    topology_hash, geometry_hash, uv_hash, settings_hash, mesh_hash = (
        _content_hashes(vertices, faces, settings_payload)
    )
    face_to_island, adjacency = _island_topology(
        mesh,
        faces,
        normalized_seams,
    )
    islands = _island_manifest(faces, face_to_island, adjacency)
    job_id = _stable_digest(
        {
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
        schema_version="1.2",
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
    )


__all__ = ("TextureJob", "build_texture_job")
