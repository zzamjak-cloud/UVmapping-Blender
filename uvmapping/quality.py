"""Blender 의존성 없이 동작하는 UV 품질 평가기."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from heapq import heappop, heappush
import json
from math import exp, isfinite, log, sqrt
from typing import Any, Callable, Mapping, Sequence


_EPSILON = 1.0e-12
_OVERLAP_EPSILON = 1.0e-10
_DEFAULT_MAX_PAIR_CHECKS = 1_000_000

OVERLAP_EXACT = "EXACT"
OVERLAP_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class UVQualityReport:
    """자동 언랩 결과를 비교할 수 있는 정규화된 품질 지표."""

    triangle_count: int
    overlap_pairs: int
    overlap_status: str
    degenerate_triangles: int
    flipped_triangles: int
    area_distortion_mean: float
    area_distortion_p95: float
    utilization: float
    seam_ratio: float
    chart_count: int
    valid: bool
    objective_score: float
    uv_bounds: tuple[Vec2, Vec2]

    def to_dict(self) -> dict[str, Any]:
        """JSON 인코더에 바로 전달할 수 있는 사전으로 변환한다."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class AtlasQualityReport:
    """최종 Atlas 공간에서 member 간 충돌과 범위를 평가한 결과."""

    member_count: int
    triangle_count: int
    overlap_pairs: int
    overlap_status: str
    out_of_bounds_count: int
    bounds: tuple[Vec2, Vec2]
    member_bounds: dict[str, tuple[Vec2, Vec2]]
    member_uv_hashes: dict[str, str]
    atlas_hash: str
    valid: bool

    def to_dict(self) -> dict[str, Any]:
        """JSON 인코더에 바로 전달할 수 있는 사전으로 변환한다."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class _FaceData:
    index: int
    vertices: tuple[int, ...]
    coordinates: tuple[Vec3, ...]
    uvs: tuple[Vec2, ...]


@dataclass(frozen=True, slots=True)
class _TriangleData:
    face_index: int
    vertices: tuple[int, int, int]
    coordinates: tuple[Vec3, Vec3, Vec3]
    uvs: tuple[Vec2, Vec2, Vec2]


def _component(value: Any, index: int) -> float:
    try:
        return float(value[index])
    except (IndexError, KeyError, TypeError):
        name = "xyz"[index] if index < 3 else str(index)
        return float(getattr(value, name))


def _vec2(value: Any) -> Vec2:
    return (_component(value, 0), _component(value, 1))


def _vec3(value: Any) -> Vec3:
    return (_component(value, 0), _component(value, 1), _component(value, 2))


def _item_index(value: Any) -> int:
    return int(value if isinstance(value, int) else value.index)


def _select_uv_layer(mesh: Any, uv_layer_name: str | None) -> tuple[str, Any]:
    layers = mesh.uv_layers
    layer = None
    if uv_layer_name is not None:
        getter = getattr(layers, "get", None)
        if getter is not None:
            layer = getter(uv_layer_name)
        else:
            layer = next(
                (item for item in layers if getattr(item, "name", None) == uv_layer_name),
                None,
            )
        if layer is None:
            raise ValueError(f"UV 레이어를 찾을 수 없습니다: {uv_layer_name!r}")
    else:
        layer = getattr(layers, "active", None)
        if layer is None:
            try:
                layer = layers[0]
            except (IndexError, KeyError, TypeError):
                layer = None
        if layer is None:
            raise ValueError("평가할 UV 레이어가 없습니다.")
    return str(getattr(layer, "name", uv_layer_name or "UVMap")), layer


def _polygon_loop_indices(polygon: Any, count: int, cursor: int) -> tuple[int, ...]:
    if hasattr(polygon, "loop_indices"):
        result = tuple(int(index) for index in polygon.loop_indices)
    elif hasattr(polygon, "loop_start"):
        start = int(polygon.loop_start)
        total = int(getattr(polygon, "loop_total", count))
        result = tuple(range(start, start + total))
    else:
        result = tuple(range(cursor, cursor + count))
    if len(result) != count:
        raise ValueError("Polygon 정점 수와 UV loop 수가 일치하지 않습니다.")
    return result


def _extract_faces(
    mesh: Any,
    uv_layer_name: str | None = None,
) -> tuple[str, tuple[_FaceData, ...], tuple[Vec3, ...]]:
    """Blender Mesh 또는 같은 속성을 가진 테스트 메시를 정규화한다."""

    layer_name, layer = _select_uv_layer(mesh, uv_layer_name)
    vertices = tuple(_vec3(vertex.co) for vertex in mesh.vertices)
    faces: list[_FaceData] = []
    loop_cursor = 0
    for position, polygon in enumerate(mesh.polygons):
        indices = tuple(_item_index(vertex) for vertex in polygon.vertices)
        loop_indices = _polygon_loop_indices(polygon, len(indices), loop_cursor)
        loop_cursor = max(loop_cursor, max(loop_indices, default=loop_cursor - 1) + 1)
        uvs = tuple(_vec2(layer.data[index].uv) for index in loop_indices)
        try:
            coordinates = tuple(vertices[index] for index in indices)
        except IndexError as exc:
            raise ValueError("Polygon이 존재하지 않는 정점을 참조합니다.") from exc
        faces.append(
            _FaceData(
                index=int(getattr(polygon, "index", position)),
                vertices=indices,
                coordinates=coordinates,
                uvs=uvs,
            )
        )
    return layer_name, tuple(faces), vertices


def _project_polygon(coordinates: Sequence[Vec3]) -> tuple[Vec2, ...]:
    """Newell normal의 주축을 제외해 Blender와 같은 평면 투영을 만든다."""

    normal = [0.0, 0.0, 0.0]
    points = tuple(coordinates)
    for current, following in zip(points, points[1:] + points[:1], strict=True):
        normal[0] += (current[1] - following[1]) * (current[2] + following[2])
        normal[1] += (current[2] - following[2]) * (current[0] + following[0])
        normal[2] += (current[0] - following[0]) * (current[1] + following[1])
    drop_axis = max(range(3), key=lambda axis: (abs(normal[axis]), -axis))
    axis_pairs = ((1, 2), (0, 2), (0, 1))
    first_axis, second_axis = axis_pairs[drop_axis]
    projected = tuple(
        (point[first_axis], point[second_axis]) for point in coordinates
    )
    if abs(_polygon_signed_area(projected)) > _EPSILON:
        return projected

    # Newell normal이 퇴화한 비평면/공선 입력은 면적이 가장 큰 투영을 쓴다.
    candidates = []
    for order, (first, second) in enumerate(axis_pairs):
        candidate = tuple((point[first], point[second]) for point in coordinates)
        candidates.append((abs(_polygon_signed_area(candidate)), -order, candidate))
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _cross_2d(first: Vec2, second: Vec2, third: Vec2) -> float:
    return (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )


def _point_in_triangle(
    point: Vec2,
    first: Vec2,
    second: Vec2,
    third: Vec2,
    orientation: float,
) -> bool:
    return all(
        orientation * value >= -_EPSILON
        for value in (
            _cross_2d(first, second, point),
            _cross_2d(second, third, point),
            _cross_2d(third, first, point),
        )
    )


def _ear_clipping_indices(coordinates: Sequence[Vec3]) -> tuple[tuple[int, int, int], ...]:
    """원래 winding을 보존하는 결정론적 ear-clipping index를 반환한다."""

    count = len(coordinates)
    if count < 3:
        return ()
    if count == 3:
        return ((0, 1, 2),)
    projected = _project_polygon(coordinates)
    signed_area = _polygon_signed_area(projected)
    if abs(signed_area) <= _EPSILON:
        return tuple((0, offset, offset + 1) for offset in range(1, count - 1))
    orientation = 1.0 if signed_area > 0.0 else -1.0
    remaining = list(range(count))
    result: list[tuple[int, int, int]] = []
    while len(remaining) > 3:
        ear_position = None
        for position, current in enumerate(remaining):
            previous = remaining[position - 1]
            following = remaining[(position + 1) % len(remaining)]
            if (
                orientation
                * _cross_2d(
                    projected[previous],
                    projected[current],
                    projected[following],
                )
                <= _EPSILON
            ):
                continue
            if any(
                _point_in_triangle(
                    projected[candidate],
                    projected[previous],
                    projected[current],
                    projected[following],
                    orientation,
                )
                for candidate in remaining
                if candidate not in {previous, current, following}
            ):
                continue
            ear_position = position
            result.append((previous, current, following))
            break
        if ear_position is None:
            # 자기 교차 또는 중복 정점 입력도 결정론적으로 종료한다.
            anchor = remaining[0]
            result.extend(
                (anchor, remaining[offset], remaining[offset + 1])
                for offset in range(1, len(remaining) - 1)
            )
            return tuple(result)
        del remaining[ear_position]
    result.append((remaining[0], remaining[1], remaining[2]))
    return tuple(result)


def _triangulate(faces: Sequence[_FaceData]) -> tuple[_TriangleData, ...]:
    """3D 형상 기준 ear-clipping으로 n-gon을 결정론적으로 삼각분할한다."""

    triangles: list[_TriangleData] = []
    for face in faces:
        for first, second, third in _ear_clipping_indices(face.coordinates):
            triangles.append(
                _TriangleData(
                    face_index=face.index,
                    vertices=(
                        face.vertices[first],
                        face.vertices[second],
                        face.vertices[third],
                    ),
                    coordinates=(
                        face.coordinates[first],
                        face.coordinates[second],
                        face.coordinates[third],
                    ),
                    uvs=(
                        face.uvs[first],
                        face.uvs[second],
                        face.uvs[third],
                    ),
                )
            )
    return tuple(triangles)


def _signed_uv_area(points: Sequence[Vec2]) -> float:
    first, second, third = points
    return 0.5 * (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )


def _triangle_area_3d(points: Sequence[Vec3]) -> float:
    first, second, third = points
    left = tuple(second[index] - first[index] for index in range(3))
    right = tuple(third[index] - first[index] for index in range(3))
    cross = (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )
    return 0.5 * sqrt(sum(component * component for component in cross))


def _aabb(points: Sequence[Vec2]) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _aabb_has_positive_intersection(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        min(first[2], second[2]) - max(first[0], second[0]) > _OVERLAP_EPSILON
        and min(first[3], second[3]) - max(first[1], second[1])
        > _OVERLAP_EPSILON
    )


def _polygon_signed_area(points: Sequence[Vec2]) -> float:
    closed = tuple(points)
    return 0.5 * sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(closed, closed[1:] + closed[:1], strict=True)
    )


def _triangle_intersection_area(subject: Sequence[Vec2], clip: Sequence[Vec2]) -> float:
    """Sutherland-Hodgman clipping으로 양의 삼각형 교차 면적을 구한다."""

    orientation = 1.0 if _polygon_signed_area(clip) >= 0.0 else -1.0
    polygon = list(subject)
    clip_points = tuple(clip)
    for clip_start, clip_end in zip(
        clip_points,
        clip_points[1:] + clip_points[:1],
        strict=True,
    ):
        if not polygon:
            return 0.0
        output: list[Vec2] = []

        def side(point: Vec2) -> float:
            return orientation * (
                (clip_end[0] - clip_start[0]) * (point[1] - clip_start[1])
                - (clip_end[1] - clip_start[1]) * (point[0] - clip_start[0])
            )

        previous = polygon[-1]
        previous_side = side(previous)
        for current in polygon:
            current_side = side(current)
            previous_inside = previous_side >= -_EPSILON
            current_inside = current_side >= -_EPSILON
            if previous_inside != current_inside:
                denominator = previous_side - current_side
                factor = previous_side / denominator if denominator else 0.0
                output.append(
                    (
                        previous[0] + factor * (current[0] - previous[0]),
                        previous[1] + factor * (current[1] - previous[1]),
                    )
                )
            if current_inside:
                output.append(current)
            previous = current
            previous_side = current_side
        polygon = output
    return abs(_polygon_signed_area(polygon)) if len(polygon) >= 3 else 0.0


def _count_overlaps(
    triangles: Sequence[_TriangleData],
    max_pair_checks: int | None,
    pair_filter: Callable[[int, int], bool] | None = None,
) -> tuple[int, str]:
    """x축 sweep-line으로 AABB 후보를 줄이고 중복 없는 pair만 검사한다."""

    bounds = tuple(_aabb(triangle.uvs) for triangle in triangles)
    non_degenerate = [
        index
        for index, triangle in enumerate(triangles)
        if abs(_signed_uv_area(triangle.uvs)) > _EPSILON
    ]
    ordered = sorted(
        non_degenerate,
        key=lambda index: (
            bounds[index][0],
            bounds[index][2],
            bounds[index][1],
            bounds[index][3],
            index,
        ),
    )
    active: dict[int, None] = {}
    expiry_heap: list[tuple[float, int]] = []
    candidate_work = 0
    overlaps = 0
    for current_index in ordered:
        current_bounds = bounds[current_index]
        while (
            expiry_heap
            and expiry_heap[0][0] - current_bounds[0] <= _OVERLAP_EPSILON
        ):
            _, expired_index = heappop(expiry_heap)
            active.pop(expired_index, None)
        if (
            max_pair_checks is not None
            and candidate_work + len(active) > max_pair_checks
        ):
            return overlaps, OVERLAP_BUDGET_EXCEEDED
        for previous_index in active:
            candidate_work += 1
            # 이전 active와 새 current 조합은 sweep에서 한 번만 생성된다.
            pair = (
                min(previous_index, current_index),
                max(previous_index, current_index),
            )
            if pair_filter is not None and not pair_filter(pair[0], pair[1]):
                continue
            if not _aabb_has_positive_intersection(
                bounds[previous_index], current_bounds
            ):
                continue
            if (
                _triangle_intersection_area(
                    triangles[previous_index].uvs,
                    triangles[current_index].uvs,
                )
                > _OVERLAP_EPSILON
            ):
                overlaps += 1
        active[current_index] = None
        heappush(expiry_heap, (current_bounds[2], current_index))
    return overlaps, OVERLAP_EXACT


def _uv_equal(first: Vec2, second: Vec2) -> bool:
    return abs(first[0] - second[0]) <= 1.0e-9 and abs(first[1] - second[1]) <= 1.0e-9


def _face_island_map(faces: Sequence[_FaceData]) -> dict[int, int]:
    """Seam 속성 없이 실제 UV 연속성으로 face island를 계산한다."""

    parent = {face.index: face.index for face in faces}

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            lower, upper = sorted((first_root, second_root))
            parent[upper] = lower

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
        for first_position, (first_face, first_uvs) in enumerate(linked):
            for second_face, second_uvs in linked[first_position + 1 :]:
                if all(_uv_equal(first_uvs[vertex], second_uvs[vertex]) for vertex in key):
                    union(first_face, second_face)
    return {face.index: find(face.index) for face in faces}


def _count_flipped_triangles(
    faces: Sequence[_FaceData],
    triangles: Sequence[_TriangleData],
    signed_areas: Sequence[float],
) -> int:
    """Island 전체 mirror는 허용하고 내부의 반대 방향 삼각형만 센다."""

    islands = _face_island_map(faces)
    orientations: dict[int, list[int]] = {}
    for triangle, area in zip(triangles, signed_areas, strict=True):
        if abs(area) <= _EPSILON:
            continue
        counts = orientations.setdefault(islands[triangle.face_index], [0, 0])
        counts[0 if area > 0.0 else 1] += 1
    return sum(min(positive, negative) for positive, negative in orientations.values())


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _uv_bounds(faces: Sequence[_FaceData]) -> tuple[Vec2, Vec2]:
    points = [uv for face in faces for uv in face.uvs]
    if not points:
        return ((0.0, 0.0), (0.0, 0.0))
    return (
        (min(point[0] for point in points), min(point[1] for point in points)),
        (max(point[0] for point in points), max(point[1] for point in points)),
    )


def evaluate_uv_quality(
    mesh: Any,
    uv_layer_name: str | None = None,
    seam_count: int = 0,
    chart_count: int = 0,
    max_pair_checks: int | None = _DEFAULT_MAX_PAIR_CHECKS,
) -> UVQualityReport:
    """Blender Mesh 또는 호환 fake mesh의 UV 품질을 평가한다."""

    if max_pair_checks is not None and max_pair_checks < 0:
        raise ValueError("max_pair_checks 값은 0 이상이거나 None이어야 합니다.")
    _, faces, _ = _extract_faces(mesh, uv_layer_name)
    triangles = _triangulate(faces)
    signed_areas = [_signed_uv_area(triangle.uvs) for triangle in triangles]
    geometry_areas = [_triangle_area_3d(triangle.coordinates) for triangle in triangles]
    uv_areas = [abs(area) for area in signed_areas]
    degenerate = sum(
        uv_area <= _EPSILON or geometry_area <= _EPSILON
        for uv_area, geometry_area in zip(uv_areas, geometry_areas, strict=True)
    )
    flipped = _count_flipped_triangles(faces, triangles, signed_areas)
    overlap_pairs, overlap_status = _count_overlaps(triangles, max_pair_checks)

    ratios = [
        uv_area / geometry_area
        for uv_area, geometry_area in zip(uv_areas, geometry_areas, strict=True)
        if uv_area > _EPSILON and geometry_area > _EPSILON
    ]
    total_geometry_area = sum(
        geometry_area
        for uv_area, geometry_area in zip(uv_areas, geometry_areas, strict=True)
        if uv_area > _EPSILON and geometry_area > _EPSILON
    )
    total_uv_area = sum(
        uv_area
        for uv_area, geometry_area in zip(uv_areas, geometry_areas, strict=True)
        if uv_area > _EPSILON and geometry_area > _EPSILON
    )
    reference_ratio = (
        total_uv_area / total_geometry_area if total_geometry_area > _EPSILON else 0.0
    )
    distortions = [
        abs(log(ratio / reference_ratio))
        for ratio in ratios
        if ratio > _EPSILON and reference_ratio > _EPSILON
    ]
    distortion_mean = sum(distortions) / len(distortions) if distortions else 0.0
    distortion_p95 = _percentile(distortions, 0.95)

    bounds = _uv_bounds(faces)
    bounds_area = (bounds[1][0] - bounds[0][0]) * (bounds[1][1] - bounds[0][1])
    utilization = (
        min(1.0, total_uv_area / bounds_area) if bounds_area > _EPSILON else 0.0
    )
    edge_count = len(getattr(mesh, "edges", ()))
    seam_ratio = (
        min(1.0, max(0.0, float(seam_count) / edge_count)) if edge_count else 0.0
    )
    valid = bool(triangles) and not (
        overlap_pairs
        or overlap_status != OVERLAP_EXACT
        or degenerate
        or flipped
        or not isfinite(distortion_mean)
    )
    defect_count = overlap_pairs + degenerate + flipped
    objective_score = utilization * exp(-distortion_mean)
    objective_score *= 1.0 - 0.15 * seam_ratio
    objective_score /= 1.0 + defect_count
    if overlap_status != OVERLAP_EXACT:
        objective_score *= 0.25
    objective_score = min(1.0, max(0.0, objective_score))

    return UVQualityReport(
        triangle_count=len(triangles),
        overlap_pairs=overlap_pairs,
        overlap_status=overlap_status,
        degenerate_triangles=degenerate,
        flipped_triangles=flipped,
        area_distortion_mean=distortion_mean,
        area_distortion_p95=distortion_p95,
        utilization=utilization,
        seam_ratio=seam_ratio,
        chart_count=max(0, int(chart_count)),
        valid=valid,
        objective_score=objective_score,
        uv_bounds=bounds,
    )


def _normalize_atlas_entry(entry: Any) -> tuple[str, Any, str | None]:
    if isinstance(entry, Mapping):
        member_id = entry.get("member_id")
        mesh = entry.get("mesh")
        uv_layer_name = entry.get("uv_layer_name")
    elif isinstance(entry, (tuple, list)) and len(entry) == 3:
        member_id, mesh, uv_layer_name = entry
    else:
        member_id = getattr(entry, "member_id", None)
        mesh = getattr(entry, "mesh", None)
        uv_layer_name = getattr(entry, "uv_layer_name", None)
    normalized_id = str(member_id).strip() if member_id is not None else ""
    if not normalized_id:
        raise ValueError("Atlas member_id는 비어 있을 수 없습니다.")
    if mesh is None:
        raise ValueError(f"Atlas member {normalized_id!r}의 mesh가 없습니다.")
    return normalized_id, mesh, None if uv_layer_name is None else str(uv_layer_name)


def _member_uv_hash(faces: Sequence[_FaceData]) -> str:
    topology_payload = {
        "faces": [
            {"index": face.index, "vertices": list(face.vertices)} for face in faces
        ]
    }
    topology_hash = sha256(
        json.dumps(
            topology_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    uv_payload = {
        "topology_hash": topology_hash,
        "faces": [
            {
                "index": face.index,
                "uvs": [
                    [
                        format(component if component != 0.0 else 0.0, ".12g")
                        for component in uv
                    ]
                    for uv in face.uvs
                ],
            }
            for face in faces
        ],
    }
    encoded = json.dumps(
        uv_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def evaluate_atlas_quality(
    entries: Sequence[tuple[str, Any, str | None] | Mapping[str, Any] | Any],
    max_pair_checks: int | None = _DEFAULT_MAX_PAIR_CHECKS,
) -> AtlasQualityReport:
    """최종 Atlas에서 서로 다른 member 사이의 overlap과 bounds를 평가한다.

    기본 entry 형식은 ``(member_id, mesh, uv_layer_name)``이다. 같은 member의
    내부 triangle pair는 단일 메시 품질 리포트의 책임이므로 여기서는 제외한다.
    """

    if max_pair_checks is not None and max_pair_checks < 0:
        raise ValueError("max_pair_checks 값은 0 이상이거나 None이어야 합니다.")
    normalized = sorted(
        (_normalize_atlas_entry(entry) for entry in entries),
        key=lambda item: item[0],
    )
    member_ids = [member_id for member_id, _, _ in normalized]
    if len(set(member_ids)) != len(member_ids):
        raise ValueError("Atlas member_id는 고유해야 합니다.")

    triangles: list[_TriangleData] = []
    triangle_members: list[str] = []
    member_bounds: dict[str, tuple[Vec2, Vec2]] = {}
    member_uv_hashes: dict[str, str] = {}
    all_faces: list[_FaceData] = []
    for member_id, mesh, uv_layer_name in normalized:
        _, faces, _ = _extract_faces(mesh, uv_layer_name)
        member_triangles = _triangulate(faces)
        triangles.extend(member_triangles)
        triangle_members.extend([member_id] * len(member_triangles))
        member_bounds[member_id] = _uv_bounds(faces)
        member_uv_hashes[member_id] = _member_uv_hash(faces)
        all_faces.extend(faces)

    overlap_pairs, overlap_status = _count_overlaps(
        triangles,
        max_pair_checks,
        pair_filter=lambda first, second: (
            triangle_members[first] != triangle_members[second]
        ),
    )
    bounds = _uv_bounds(all_faces)
    out_of_bounds = sum(
        lower[0] < -_EPSILON
        or lower[1] < -_EPSILON
        or upper[0] > 1.0 + _EPSILON
        or upper[1] > 1.0 + _EPSILON
        for lower, upper in member_bounds.values()
    )
    atlas_hash_payload = {
        "members": [
            {"member_id": member_id, "uv_hash": member_uv_hashes[member_id]}
            for member_id in member_ids
        ]
    }
    atlas_hash = sha256(
        json.dumps(
            atlas_hash_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    valid = bool(normalized) and bool(triangles) and not (
        overlap_pairs
        or overlap_status != OVERLAP_EXACT
        or out_of_bounds
    )
    return AtlasQualityReport(
        member_count=len(normalized),
        triangle_count=len(triangles),
        overlap_pairs=overlap_pairs,
        overlap_status=overlap_status,
        out_of_bounds_count=out_of_bounds,
        bounds=bounds,
        member_bounds=member_bounds,
        member_uv_hashes=member_uv_hashes,
        atlas_hash=atlas_hash,
        valid=valid,
    )


__all__ = (
    "OVERLAP_BUDGET_EXCEEDED",
    "OVERLAP_EXACT",
    "AtlasQualityReport",
    "UVQualityReport",
    "evaluate_atlas_quality",
    "evaluate_uv_quality",
)
