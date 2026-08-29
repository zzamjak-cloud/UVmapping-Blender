"""Blender Mesh/BMesh를 읽어 결정론적인 자동 Seam 후보를 계산한다.

Blender 데이터는 읽기만 하며 Seam 속성이나 UV 레이어를 직접 변경하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
from math import atan2, exp, sqrt
from statistics import median
from typing import Any

from .metrics import (
    boundary_vertex_groups,
    chart_face_counts,
    count_charts,
    face_chart_ids,
    face_components,
)
from .types import AnalysisOptions, AnalysisResult

Vec3 = tuple[float, float, float]


@dataclass(slots=True)
class _Face:
    position: int
    vertices: tuple[int, ...]
    edge_positions: tuple[int, ...]
    normal: Vec3
    material_index: int


@dataclass(slots=True)
class _Edge:
    position: int
    result_index: int
    vertices: tuple[int, int]
    use_seam: bool
    is_sharp: bool
    length: float
    linked_faces: list[int] = field(default_factory=list)


@dataclass(slots=True)
class _MeshData:
    vertices: list[Vec3]
    edges: list[_Edge]
    faces: list[_Face]
    warnings: list[str]

    @property
    def edge_faces(self) -> list[list[int]]:
        return [edge.linked_faces for edge in self.edges]

    @property
    def edge_vertices(self) -> list[tuple[int, int]]:
        return [edge.vertices for edge in self.edges]


def _vec3(value: Any) -> Vec3:
    return (float(value[0]), float(value[1]), float(value[2]))


def _subtract(first: Vec3, second: Vec3) -> Vec3:
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _dot(first: Vec3, second: Vec3) -> float:
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _cross(first: Vec3, second: Vec3) -> Vec3:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _length(vector: Vec3) -> float:
    return sqrt(_dot(vector, vector))


def _normalized(vector: Vec3) -> Vec3:
    magnitude = _length(vector)
    if magnitude <= 1.0e-12:
        return (0.0, 0.0, 0.0)
    return (vector[0] / magnitude, vector[1] / magnitude, vector[2] / magnitude)


def _face_normal(vertices: tuple[int, ...], coordinates: list[Vec3]) -> Vec3:
    """Newell 방식으로 삼각형과 N-gon의 안정적인 법선을 계산한다."""

    if len(vertices) < 3:
        return (0.0, 0.0, 0.0)
    normal = [0.0, 0.0, 0.0]
    for position, vertex in enumerate(vertices):
        current = coordinates[vertex]
        following = coordinates[vertices[(position + 1) % len(vertices)]]
        normal[0] += (current[1] - following[1]) * (current[2] + following[2])
        normal[1] += (current[2] - following[2]) * (current[0] + following[0])
        normal[2] += (current[0] - following[0]) * (current[1] + following[1])
    return _normalized((normal[0], normal[1], normal[2]))


def _declared_indices(items: list[Any]) -> list[int]:
    declared = [int(getattr(item, "index", -1)) for item in items]
    if all(index >= 0 for index in declared) and len(set(declared)) == len(declared):
        return declared
    return list(range(len(items)))


def _normalize_mesh(mesh: Any) -> _MeshData:
    """bpy.types.Mesh 또는 BMesh 유사 객체를 작은 내부 표현으로 변환한다."""

    is_bmesh = hasattr(mesh, "verts") and hasattr(mesh, "faces")
    vertex_items = list(mesh.verts if is_bmesh else mesh.vertices)
    edge_items = list(mesh.edges)
    face_items = list(mesh.faces if is_bmesh else mesh.polygons)
    warnings: list[str] = []

    vertex_declared = _declared_indices(vertex_items)
    vertex_by_object = {id(item): position for position, item in enumerate(vertex_items)}
    vertex_by_index = {index: position for position, index in enumerate(vertex_declared)}
    coordinates = [_vec3(vertex.co) for vertex in vertex_items]

    def vertex_position(reference: Any) -> int:
        if isinstance(reference, int):
            if reference in vertex_by_index:
                return vertex_by_index[reference]
            if 0 <= reference < len(vertex_items):
                return reference
        object_position = vertex_by_object.get(id(reference))
        if object_position is not None:
            return object_position
        declared = int(getattr(reference, "index", -1))
        if declared in vertex_by_index:
            return vertex_by_index[declared]
        raise ValueError("Face 또는 Edge가 메시 밖의 Vertex를 참조합니다.")

    edge_declared = _declared_indices(edge_items)
    edge_by_object = {id(item): position for position, item in enumerate(edge_items)}
    mesh_edge_flags: dict[str, list[bool]] = {}
    if not is_bmesh and hasattr(mesh, "attributes"):
        for attribute_name in ("uv_seam", "sharp_edge"):
            attribute = mesh.attributes.get(attribute_name)
            if (
                attribute is not None
                and attribute.domain == "EDGE"
                and attribute.data_type == "BOOLEAN"
            ):
                mesh_edge_flags[attribute_name] = [
                    bool(value.value) for value in attribute.data
                ]
    edges: list[_Edge] = []
    edge_by_key: dict[tuple[int, int], int] = {}
    for position, item in enumerate(edge_items):
        references = list(item.verts if hasattr(item, "verts") else item.vertices)
        if len(references) != 2:
            raise ValueError("Edge는 정확히 두 Vertex를 가져야 합니다.")
        first = vertex_position(references[0])
        second = vertex_position(references[1])
        key = tuple(sorted((first, second)))
        edge_by_key.setdefault(key, position)
        use_seam = bool(getattr(item, "seam", getattr(item, "use_seam", False)))
        if "uv_seam" in mesh_edge_flags:
            use_seam = mesh_edge_flags["uv_seam"][position]
        if hasattr(item, "smooth"):
            is_sharp = not bool(item.smooth)
        elif "sharp_edge" in mesh_edge_flags:
            is_sharp = mesh_edge_flags["sharp_edge"][position]
        else:
            is_sharp = bool(getattr(item, "use_edge_sharp", False))
        edge_length = _length(_subtract(coordinates[second], coordinates[first]))
        edges.append(
            _Edge(
                position=position,
                result_index=edge_declared[position],
                vertices=(first, second),
                use_seam=use_seam,
                is_sharp=is_sharp,
                length=edge_length,
            )
        )

    faces: list[_Face] = []
    missing_face_edges = 0
    for position, item in enumerate(face_items):
        references = list(item.verts if hasattr(item, "verts") else item.vertices)
        vertices = tuple(vertex_position(reference) for reference in references)
        if hasattr(item, "edges"):
            edge_positions = tuple(
                edge_by_object[id(edge)] for edge in item.edges if id(edge) in edge_by_object
            )
        else:
            found: list[int] = []
            for offset, first in enumerate(vertices):
                second = vertices[(offset + 1) % len(vertices)]
                edge_position = edge_by_key.get(tuple(sorted((first, second))))
                if edge_position is None:
                    missing_face_edges += 1
                    continue
                found.append(edge_position)
            edge_positions = tuple(found)
        normal = _face_normal(vertices, coordinates)
        faces.append(
            _Face(
                position=position,
                vertices=vertices,
                edge_positions=edge_positions,
                normal=normal,
                material_index=int(getattr(item, "material_index", 0)),
            )
        )
        for edge_position in edge_positions:
            edges[edge_position].linked_faces.append(position)

    if missing_face_edges:
        warnings.append(f"Face 경계에 대응하는 Edge {missing_face_edges}개를 찾지 못했습니다.")
    return _MeshData(coordinates, edges, faces, warnings)


def _oriented_edge_vector(edge: _Edge, face: _Face, vertices: list[Vec3]) -> Vec3:
    for position, first in enumerate(face.vertices):
        second = face.vertices[(position + 1) % len(face.vertices)]
        if {first, second} == set(edge.vertices):
            return _normalized(_subtract(vertices[second], vertices[first]))
    first, second = edge.vertices
    return _normalized(_subtract(vertices[second], vertices[first]))


def _signed_face_angle(edge: _Edge, data: _MeshData) -> float:
    """낮은 Face 위치의 winding을 기준으로 부호 있는 이면각을 반환한다."""

    if len(edge.linked_faces) != 2:
        return 0.0
    first_face, second_face = sorted(edge.linked_faces)
    first = data.faces[first_face]
    second = data.faces[second_face]
    direction = _oriented_edge_vector(edge, first, data.vertices)
    sine = _dot(direction, _cross(first.normal, second.normal))
    cosine = max(-1.0, min(1.0, _dot(first.normal, second.normal)))
    return atan2(sine, cosine)


def _score_edges(data: _MeshData, options: AnalysisOptions) -> dict[int, float]:
    positive_lengths = [edge.length for edge in data.edges if edge.length > 1.0e-12]
    typical_length = median(positive_lengths) if positive_lengths else 1.0
    scores: dict[int, float] = {}

    for edge in data.edges:
        linked_count = len(edge.linked_faces)
        boundary_signal = 1.0 if linked_count == 1 else 0.0
        non_manifold_signal = 1.0 if linked_count == 0 or linked_count > 2 else 0.0
        material_signal = 0.0
        if linked_count == 2:
            first, second = edge.linked_faces
            material_signal = float(
                data.faces[first].material_index != data.faces[second].material_index
            )
        angle = _signed_face_angle(edge, data)
        angle_signal = min(abs(angle) / options.angle_reference, 1.0)
        # 음수 이면각을 오목한 골로 간주해 눈에 덜 띄는 Seam을 우선한다.
        angle_multiplier = options.concave_multiplier if angle < 0.0 else 1.0
        length_signal = typical_length / (typical_length + max(edge.length, 1.0e-12))

        raw_score = (
            options.angle_weight * angle_signal * angle_multiplier
            + options.length_weight * length_signal
            + options.sharp_weight * float(edge.is_sharp)
            + options.material_weight * material_signal
            + options.existing_seam_weight * float(edge.use_seam)
            + options.boundary_weight * boundary_signal
            + options.non_manifold_weight * non_manifold_signal
        )
        scores[edge.result_index] = max(0.0, min(1.0, 1.0 - exp(-raw_score)))
    return scores


def _initial_seams(
    data: _MeshData,
    scores: dict[int, float],
    options: AnalysisOptions,
) -> set[int]:
    selected: set[int] = set()
    for edge in data.edges:
        mandatory_topology = len(edge.linked_faces) != 2
        preserved = options.preserve_existing_seams and edge.use_seam
        if mandatory_topology or preserved or scores[edge.result_index] >= options.seam_threshold:
            selected.add(edge.position)
    return selected


def _component_edge_positions(data: _MeshData, component: set[int]) -> list[int]:
    return [
        edge.position
        for edge in data.edges
        if any(face in component for face in edge.linked_faces)
    ]


def _edge_cost(edge: _Edge, scores: dict[int, float]) -> float:
    """짧고 점수가 높은 Edge를 선호하는 양의 절단 비용."""

    return max(edge.length, 1.0e-9) * (1.5 - scores[edge.result_index])


def _dijkstra(
    data: _MeshData,
    allowed_edges: list[int],
    sources: set[int],
    scores: dict[int, float],
) -> tuple[dict[int, float], dict[int, tuple[int, int]]]:
    adjacency: dict[int, list[tuple[int, int]]] = {}
    for edge_position in allowed_edges:
        edge = data.edges[edge_position]
        first, second = edge.vertices
        adjacency.setdefault(first, []).append((second, edge_position))
        adjacency.setdefault(second, []).append((first, edge_position))
    for neighbors in adjacency.values():
        neighbors.sort(key=lambda item: (item[0], data.edges[item[1]].result_index))

    distances = {source: 0.0 for source in sources}
    previous: dict[int, tuple[int, int]] = {}
    queue = [(0.0, source) for source in sorted(sources)]
    heapq.heapify(queue)
    while queue:
        distance, vertex = heapq.heappop(queue)
        if distance > distances.get(vertex, float("inf")) + 1.0e-12:
            continue
        for neighbor, edge_position in adjacency.get(vertex, ()):
            candidate = distance + _edge_cost(data.edges[edge_position], scores)
            current = distances.get(neighbor, float("inf"))
            candidate_previous = (vertex, edge_position)
            if candidate < current - 1.0e-12 or (
                abs(candidate - current) <= 1.0e-12
                and candidate_previous < previous.get(neighbor, (10**18, 10**18))
            ):
                distances[neighbor] = candidate
                previous[neighbor] = candidate_previous
                heapq.heappush(queue, (candidate, neighbor))
    return distances, previous


def _shortest_path_between_sets(
    data: _MeshData,
    allowed_edges: list[int],
    sources: set[int],
    targets: set[int],
    scores: dict[int, float],
) -> tuple[list[int], list[int]]:
    distances, previous = _dijkstra(data, allowed_edges, sources, scores)
    reachable = [target for target in targets if target in distances]
    if not reachable:
        return [], []
    target = min(reachable, key=lambda vertex: (distances[vertex], vertex))
    path_edges: list[int] = []
    path_vertices = [target]
    cursor = target
    while cursor not in sources:
        parent = previous.get(cursor)
        if parent is None:
            return [], []
        parent_vertex, edge_position = parent
        path_edges.append(edge_position)
        cursor = parent_vertex
        path_vertices.append(cursor)
    path_edges.reverse()
    path_vertices.reverse()
    return path_edges, path_vertices


def _connect_boundary_groups(
    data: _MeshData,
    component: set[int],
    groups: list[set[int]],
    seam_positions: set[int],
    scores: dict[int, float],
) -> int:
    if len(groups) <= 1:
        return 0
    allowed_edges = _component_edge_positions(data, component)
    connected_vertices = set(min(groups, key=lambda group: (min(group), len(group))))
    remaining = [group for group in groups if group != connected_vertices]
    added = 0
    while remaining:
        best: tuple[float, int, list[int], list[int], set[int]] | None = None
        for group in remaining:
            path_edges, path_vertices = _shortest_path_between_sets(
                data, allowed_edges, connected_vertices, group, scores
            )
            if not path_edges:
                continue
            cost = sum(_edge_cost(data.edges[position], scores) for position in path_edges)
            candidate = (cost, min(group), path_edges, path_vertices, group)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            break
        _, _, path_edges, path_vertices, group = best
        before = len(seam_positions)
        seam_positions.update(path_edges)
        added += len(seam_positions) - before
        connected_vertices.update(group)
        connected_vertices.update(path_vertices)
        remaining.remove(group)
    return added


def _diameter_cut_path(
    data: _MeshData,
    component: set[int],
    seam_positions: set[int],
    scores: dict[int, float],
) -> int:
    allowed_edges = _component_edge_positions(data, component)
    component_vertices = {
        vertex
        for edge_position in allowed_edges
        for vertex in data.edges[edge_position].vertices
    }
    if len(component_vertices) < 2:
        return 0
    start = min(component_vertices)
    distances, _ = _dijkstra(data, allowed_edges, {start}, scores)
    if len(distances) < 2:
        return 0
    first = max(distances, key=lambda vertex: (distances[vertex], -vertex))
    distances, _ = _dijkstra(data, allowed_edges, {first}, scores)
    second = max(distances, key=lambda vertex: (distances[vertex], -vertex))
    path_edges, _ = _shortest_path_between_sets(
        data, allowed_edges, {first}, {second}, scores
    )
    before = len(seam_positions)
    seam_positions.update(path_edges)
    return len(seam_positions) - before


def _add_handle_cycles(
    data: _MeshData,
    component: set[int],
    genus: int,
    seam_positions: set[int],
    scores: dict[int, float],
    boundary_vertices: set[int] | None = None,
) -> int:
    """tree-cotree 분해로 손잡이를 여는 연결 Cut Graph를 추가한다.

    저비용 primal tree와 고비용 dual tree에 모두 포함되지 않은 ``2g``개
    Edge를 찾고, 그 끝점만 primal tree의 최소 부분 트리로 연결한다.
    """

    if genus <= 0:
        return 0
    allowed_edges = _component_edge_positions(data, component)
    vertices = {vertex for position in allowed_edges for vertex in data.edges[position].vertices}
    if not vertices:
        return 0
    boundary_anchors = vertices.intersection(boundary_vertices or ())
    root = min(boundary_anchors) if boundary_anchors else min(vertices)
    _, previous = _dijkstra(data, allowed_edges, {root}, scores)
    primal_tree = {edge_position for _, edge_position in previous.values()}

    parent = {face: face for face in component}

    def find(face: int) -> int:
        while parent[face] != face:
            parent[face] = parent[parent[face]]
            face = parent[face]
        return face

    def union(first: int, second: int) -> bool:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return False
        if first_root > second_root:
            first_root, second_root = second_root, first_root
        parent[second_root] = first_root
        return True

    dual_candidates = [
        position
        for position in allowed_edges
        if position not in primal_tree
        and len(data.edges[position].linked_faces) == 2
        and all(face in component for face in data.edges[position].linked_faces)
    ]
    # Dual tree에는 절단 비용이 큰 Edge부터 넣어 저비용 Edge가 generator로 남게 한다.
    dual_candidates.sort(
        key=lambda position: (
            -_edge_cost(data.edges[position], scores),
            data.edges[position].result_index,
        )
    )
    dual_tree: set[int] = set()
    for position in dual_candidates:
        first_face, second_face = data.edges[position].linked_faces
        if union(first_face, second_face):
            dual_tree.add(position)

    generators = sorted(
        (
            position
            for position in allowed_edges
            if position not in primal_tree
            and position not in dual_tree
            and len(data.edges[position].linked_faces) == 2
        ),
        key=lambda position: (
            _edge_cost(data.edges[position], scores),
            data.edges[position].result_index,
        ),
    )[: 2 * genus]
    if not generators:
        return 0

    terminals = {
        vertex for position in generators for vertex in data.edges[position].vertices
    }
    if boundary_anchors:
        terminals.add(root)
    tree_subset: set[int] = set()
    for terminal in sorted(terminals):
        cursor = terminal
        while cursor in previous:
            parent_vertex, edge_position = previous[cursor]
            tree_subset.add(edge_position)
            cursor = parent_vertex

    # 루트까지 합친 가지 중 generator 끝점을 잇는 데 필요 없는 잎을 제거한다.
    changed = True
    while changed:
        changed = False
        degree: dict[int, int] = {}
        for position in tree_subset:
            first, second = data.edges[position].vertices
            degree[first] = degree.get(first, 0) + 1
            degree[second] = degree.get(second, 0) + 1
        removable = []
        for position in tree_subset:
            first, second = data.edges[position].vertices
            if (degree.get(first) == 1 and first not in terminals) or (
                degree.get(second) == 1 and second not in terminals
            ):
                removable.append(position)
        if removable:
            tree_subset.difference_update(removable)
            changed = True

    before = len(seam_positions)
    seam_positions.update(generators)
    seam_positions.update(tree_subset)
    return len(seam_positions) - before


def _component_genus(data: _MeshData, component: set[int], boundary_count: int) -> int:
    edge_positions = _component_edge_positions(data, component)
    vertices = {vertex for position in edge_positions for vertex in data.edges[position].vertices}
    euler_characteristic = len(vertices) - len(edge_positions) + len(component)
    estimate = (2 - boundary_count - euler_characteristic) / 2.0
    return max(0, int(round(estimate)))


def _ensure_topology_cuts(
    data: _MeshData,
    seam_positions: set[int],
    scores: dict[int, float],
    options: AnalysisOptions,
    warnings: list[str],
) -> None:
    for component_number, component in enumerate(
        face_components(len(data.faces), data.edge_faces), start=1
    ):
        groups = boundary_vertex_groups(
            component, data.edge_vertices, data.edge_faces
        )
        if options.connect_boundary_loops and len(groups) > 1:
            added = _connect_boundary_groups(
                data, component, groups, seam_positions, scores
            )
            if added:
                warnings.append(
                    f"컴포넌트 {component_number}의 경계 {len(groups)}개를 "
                    f"최소비용 절단 경로로 연결했습니다."
                )

        internal_seams = {
            position
            for position in seam_positions
            if len(data.edges[position].linked_faces) == 2
            and all(face in component for face in data.edges[position].linked_faces)
        }
        if options.ensure_cut_paths and not groups and not internal_seams:
            added = _diameter_cut_path(data, component, seam_positions, scores)
            if added:
                warnings.append(
                    f"닫힌 컴포넌트 {component_number}에 결정론적 절단 경로를 추가했습니다."
                )

        genus = _component_genus(data, component, len(groups))
        if options.ensure_cut_paths and genus > 0:
            added = _add_handle_cycles(
                data,
                component,
                genus,
                seam_positions,
                scores,
                set().union(*groups) if groups else None,
            )
            if added:
                warnings.append(
                    f"컴포넌트 {component_number}의 손잡이 구조(genus {genus})에 "
                    f"추가 순환 절단을 적용했습니다."
                )


def _merge_small_charts(
    data: _MeshData,
    seam_positions: set[int],
    scores: dict[int, float],
    options: AnalysisOptions,
) -> int:
    protected = {
        edge.position
        for edge in data.edges
        if options.preserve_existing_seams and edge.use_seam
    }
    chart_ids = face_chart_ids(len(data.faces), data.edge_faces, seam_positions)
    counts = chart_face_counts(chart_ids)
    parent = {chart_id: chart_id for chart_id in counts}
    sizes = dict(counts)

    def find(chart_id: int) -> int:
        while parent[chart_id] != chart_id:
            parent[chart_id] = parent[parent[chart_id]]
            chart_id = parent[chart_id]
        return chart_id

    def union(first: int, second: int) -> int:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return first_root
        if sizes[first_root] < sizes[second_root] or (
            sizes[first_root] == sizes[second_root] and first_root > second_root
        ):
            first_root, second_root = second_root, first_root
        parent[second_root] = first_root
        sizes[first_root] += sizes[second_root]
        return first_root

    candidates = sorted(
        (
            scores[edge.result_index],
            edge.result_index,
            edge.position,
            edge.linked_faces[0],
            edge.linked_faces[1],
        )
        for edge in data.edges
        if edge.position in seam_positions
        and edge.position not in protected
        and len(edge.linked_faces) == 2
    )

    removed = 0
    for _, _, position, first_face, second_face in candidates:
        first_root = find(chart_ids[first_face])
        second_root = find(chart_ids[second_face])
        if first_root == second_root:
            continue
        if (
            sizes[first_root] >= options.min_chart_faces
            and sizes[second_root] >= options.min_chart_faces
        ):
            continue
        seam_positions.remove(position)
        union(first_root, second_root)
        removed += 1
    return removed


def analyze_mesh(
    mesh: Any,
    options: AnalysisOptions | None = None,
) -> AnalysisResult:
    """메시를 변경하지 않고 자동 Seam 후보와 예상 Chart 수를 반환한다.

    ``mesh``는 ``bpy.types.Mesh`` 또는 index/co/vertices/edges/faces 속성을
    제공하는 BMesh 유사 객체일 수 있다.
    """

    resolved_options = options or AnalysisOptions()
    if not isinstance(resolved_options, AnalysisOptions):
        raise TypeError("options는 AnalysisOptions 또는 None이어야 합니다.")

    data = _normalize_mesh(mesh)
    warnings = list(data.warnings)
    if not data.faces:
        warnings.append("분석할 Face가 없습니다.")
    if not data.edges:
        warnings.append("분석할 Edge가 없습니다.")
        return AnalysisResult(warnings=warnings)

    non_manifold_count = sum(
        len(edge.linked_faces) == 0 or len(edge.linked_faces) > 2 for edge in data.edges
    )
    if non_manifold_count:
        warnings.append(
            f"비매니폴드 또는 고립 Edge {non_manifold_count}개를 강제 Seam 후보로 포함했습니다."
        )

    edge_scores = _score_edges(data, resolved_options)
    seam_positions = _initial_seams(data, edge_scores, resolved_options)
    _ensure_topology_cuts(
        data, seam_positions, edge_scores, resolved_options, warnings
    )
    removed = _merge_small_charts(
        data, seam_positions, edge_scores, resolved_options
    )
    if removed:
        warnings.append(
            f"작은 UV Island를 줄이기 위해 저점수 Seam {removed}개를 병합했습니다."
        )

    seam_edges = {data.edges[position].result_index for position in seam_positions}
    chart_count = count_charts(len(data.faces), data.edge_faces, seam_positions)
    return AnalysisResult(
        seam_edges=seam_edges,
        edge_scores=edge_scores,
        chart_count=chart_count,
        warnings=warnings,
    )


__all__ = ["AnalysisOptions", "AnalysisResult", "analyze_mesh"]
