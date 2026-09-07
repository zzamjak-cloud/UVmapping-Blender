"""Blender Mesh/BMesh를 읽어 결정론적인 자동 Seam 후보를 계산한다.

Blender 데이터는 읽기만 하며 Seam 속성이나 UV 레이어를 직접 변경하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import heapq
from math import atan2, cos, exp, pi, radians, sqrt
from statistics import median
from typing import Any

from .metrics import (
    boundary_vertex_groups,
    chart_face_counts,
    count_charts,
    face_chart_ids,
    face_components,
)
from .types import AnalysisOptions, AnalysisPreset, AnalysisResult

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


def _vertex_angle_defects(data: _MeshData) -> list[float]:
    """Face 코너각을 누적해 Vertex별 각결손(이산 가우스 곡률)을 구한다."""

    sums = [0.0] * len(data.vertices)
    counts = [0] * len(data.vertices)
    for face in data.faces:
        corner_count = len(face.vertices)
        if corner_count < 3:
            continue
        for offset, vertex in enumerate(face.vertices):
            current = data.vertices[vertex]
            toward_previous = _subtract(data.vertices[face.vertices[offset - 1]], current)
            toward_next = _subtract(
                data.vertices[face.vertices[(offset + 1) % corner_count]], current
            )
            angle = atan2(
                _length(_cross(toward_previous, toward_next)),
                _dot(toward_previous, toward_next),
            )
            sums[vertex] += angle
            counts[vertex] += 1
    return [
        (2.0 * pi) - angle_sum if count else 0.0
        for angle_sum, count in zip(sums, counts, strict=True)
    ]


def _normal_region_clusters(
    data: _MeshData,
    face_positions: list[int],
    blocked_edges: set[int],
    angle_limit: float,
) -> dict[int, int]:
    """법선 원뿔 한계 안에서 인접 Face를 결정론적으로 클러스터링한다."""

    face_set = set(face_positions)
    cosine_limit = cos(min(max(angle_limit, 1.0e-3), pi))
    assignment: dict[int, int] = {}
    cluster_id = 0
    for seed in sorted(face_set):
        if seed in assignment:
            continue
        normal_sum = list(data.faces[seed].normal)
        assignment[seed] = cluster_id
        frontier: list[int] = []
        queued = {seed}

        def push_neighbors(face_position: int) -> None:
            for edge_position in data.faces[face_position].edge_positions:
                edge = data.edges[edge_position]
                if edge_position in blocked_edges or len(edge.linked_faces) != 2:
                    continue
                for neighbor in edge.linked_faces:
                    if (
                        neighbor != face_position
                        and neighbor in face_set
                        and neighbor not in assignment
                        and neighbor not in queued
                    ):
                        queued.add(neighbor)
                        heapq.heappush(frontier, neighbor)

        push_neighbors(seed)
        while frontier:
            face_position = heapq.heappop(frontier)
            if face_position in assignment:
                continue
            average = _normalized((normal_sum[0], normal_sum[1], normal_sum[2]))
            if _dot(data.faces[face_position].normal, average) < cosine_limit:
                # 이 클러스터에서는 거부하되, 이후 다른 시드의 클러스터에 남긴다.
                continue
            assignment[face_position] = cluster_id
            normal = data.faces[face_position].normal
            normal_sum[0] += normal[0]
            normal_sum[1] += normal[1]
            normal_sum[2] += normal[2]
            push_neighbors(face_position)
        cluster_id += 1
    return assignment


def _split_high_curvature_charts(
    data: _MeshData,
    seam_positions: set[int],
    options: AnalysisOptions,
    warnings: list[str],
) -> None:
    """누적 곡률이 큰 Chart를 법선 클러스터 경계 Seam으로 분할한다.

    내부 각결손 총량이 큰 Chart는 어떤 언랩으로도 텍스처 밀도를 균일하게
    만들 수 없으므로, 펼치기 전에 곡률이 경계로 방출되도록 절단한다.
    """

    if not options.split_curved_charts:
        return
    defects = _vertex_angle_defects(data)
    seam_vertices: set[int] = set()
    for position in seam_positions:
        seam_vertices.update(data.edges[position].vertices)

    chart_ids = face_chart_ids(len(data.faces), data.edge_faces, seam_positions)
    chart_faces: dict[int, list[int]] = {}
    vertex_face: dict[int, int] = {}
    for face in data.faces:
        chart_faces.setdefault(chart_ids[face.position], []).append(face.position)
        for vertex in face.vertices:
            vertex_face.setdefault(vertex, face.position)

    # Seam(경계·비매니폴드 포함)에 닿지 않는 내부 Vertex의 곡률만 합산한다.
    chart_curvature: dict[int, float] = {}
    for vertex, face_position in vertex_face.items():
        if vertex in seam_vertices:
            continue
        chart = chart_ids[face_position]
        chart_curvature[chart] = chart_curvature.get(chart, 0.0) + abs(defects[vertex])

    split_count = 0
    added_edges = 0
    for chart, face_positions in sorted(chart_faces.items()):
        if len(face_positions) < 2:
            continue
        if chart_curvature.get(chart, 0.0) <= options.max_chart_curvature:
            continue
        clusters = _normal_region_clusters(
            data, face_positions, seam_positions, options.segmentation_angle_limit
        )
        if len(set(clusters.values())) < 2:
            continue
        chart_edges = {
            edge_position
            for face_position in face_positions
            for edge_position in data.faces[face_position].edge_positions
        }
        before = len(seam_positions)
        for edge_position in sorted(chart_edges):
            edge = data.edges[edge_position]
            if edge_position in seam_positions or len(edge.linked_faces) != 2:
                continue
            first_face, second_face = edge.linked_faces
            if clusters.get(first_face) != clusters.get(second_face):
                seam_positions.add(edge_position)
        added = len(seam_positions) - before
        if added:
            split_count += 1
            added_edges += added
    if split_count:
        warnings.append(
            f"누적 곡률이 높은 Chart {split_count}개를 "
            f"법선 클러스터 경계 Seam {added_edges}개로 분할했습니다."
        )


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


def detect_analysis_preset(mesh: Any) -> AnalysisPreset:
    """이면각·Sharp·재질 경계 비율로 메시 성격에 맞는 프리셋을 고른다.

    피처 Edge(뚜렷한 이면각, Sharp 표시, 재질 경계)가 많으면 하드서페이스,
    거의 없으면 유기체, 그 사이면 균형 프리셋을 반환한다. 평면 Edge가
    많으면 베벨·서브디비전으로 매끄러워진 하드서페이스로 보고 유기체로
    분류하지 않는다.
    """

    data = _normalize_mesh(mesh)
    manifold_edges = [edge for edge in data.edges if len(edge.linked_faces) == 2]
    if not manifold_edges:
        return AnalysisPreset.BALANCED

    feature_angle = radians(35.0)
    planar_angle = radians(2.0)
    feature_count = 0
    planar_count = 0
    for edge in manifold_edges:
        first, second = edge.linked_faces
        material_boundary = (
            data.faces[first].material_index != data.faces[second].material_index
        )
        angle = abs(_signed_face_angle(edge, data))
        if edge.is_sharp or material_boundary or angle >= feature_angle:
            feature_count += 1
        elif angle <= planar_angle:
            planar_count += 1

    feature_ratio = feature_count / len(manifold_edges)
    planar_ratio = planar_count / len(manifold_edges)
    if feature_ratio >= 0.15:
        return AnalysisPreset.HARD_SURFACE
    if planar_ratio >= 0.4:
        return AnalysisPreset.BALANCED
    if feature_ratio <= 0.03:
        return AnalysisPreset.ORGANIC
    return AnalysisPreset.BALANCED


def resolve_auto_quality_level(face_count: int) -> str:
    """Face 수에 맞춰 평가할 후보 수 단계를 고른다.

    작은 메시는 후보 비교 비용이 싸므로 품질 우선, 큰 메시는 빠르게 처리한다.
    """

    if face_count <= 4000:
        return "QUALITY"
    if face_count <= 30000:
        return "BALANCED"
    return "FAST"


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
    # 곡률 분할을 위상 절단보다 먼저 수행해, 매끈한 닫힌 메시가
    # 지름 절단 경로 하나로 찌그러진 단일 Chart가 되는 것을 막는다.
    _split_high_curvature_charts(data, seam_positions, resolved_options, warnings)
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


def generate_analysis_candidates(
    mesh: Any,
    base_options: AnalysisOptions | None = None,
    quality_level: str = "BALANCED",
) -> list[AnalysisResult]:
    """보수성이 점진적으로 커지는 결정론적 Seam 후보를 반환한다.

    ``FAST``는 기본 설정 한 개만 평가한다. ``BALANCED``와 ``QUALITY``는
    각각 최대 3개와 5개를 평가하되, 동일한 Seam 집합은 한 번만 반환한다.
    ``QUALITY``의 조밀 안전 후보는 기본 후보보다 낮은 임계값을 사용하고
    기본 Seam을 합쳐 위상 필수 절단을 잃지 않는다.
    """

    resolved_options = base_options or AnalysisOptions()
    if not isinstance(resolved_options, AnalysisOptions):
        raise TypeError("base_options는 AnalysisOptions 또는 None이어야 합니다.")

    raw_quality = getattr(quality_level, "value", quality_level)
    normalized_quality = (
        str(raw_quality).strip().upper().replace("-", "_").replace(" ", "_")
    )
    candidate_limits = {"FAST": 1, "BALANCED": 3, "QUALITY": 5}
    try:
        candidate_limit = candidate_limits[normalized_quality]
    except KeyError as exc:
        choices = ", ".join(candidate_limits)
        raise ValueError(
            f"지원하지 않는 품질 단계입니다: {quality_level!r} ({choices})"
        ) from exc

    base_result = analyze_mesh(mesh, resolved_options)
    base_result.options = resolved_options
    base_result.candidate_label = "기본"

    candidate_specs: list[tuple[str, AnalysisOptions, str]] = []
    if normalized_quality == "QUALITY":
        candidate_specs.append(
            (
                "조밀 안전",
                replace(
                    resolved_options,
                    seam_threshold=max(
                        0.0, resolved_options.seam_threshold - 0.12
                    ),
                    max_chart_curvature=(
                        resolved_options.max_chart_curvature * 0.75
                    ),
                    segmentation_angle_limit=(
                        resolved_options.segmentation_angle_limit * 0.85
                    ),
                ),
                "dense",
            )
        )

    conservative_count = candidate_limit - 1
    if normalized_quality == "QUALITY":
        conservative_count -= 1
    chart_face_increment = max(1, (resolved_options.min_chart_faces + 1) // 2)
    for step in range(1, conservative_count + 1):
        candidate_specs.append(
            (
                f"보수 {step}",
                replace(
                    resolved_options,
                    seam_threshold=min(
                        1.0, resolved_options.seam_threshold + 0.06 * step
                    ),
                    angle_reference=(
                        resolved_options.angle_reference * (1.0 + 0.15 * step)
                    ),
                    min_chart_faces=(
                        resolved_options.min_chart_faces
                        + chart_face_increment * step
                    ),
                    max_chart_curvature=(
                        resolved_options.max_chart_curvature * (1.0 + 0.3 * step)
                    ),
                    segmentation_angle_limit=min(
                        radians(150.0),
                        resolved_options.segmentation_angle_limit
                        * (1.0 + 0.15 * step),
                    ),
                ),
                "conservative",
            )
        )

    candidates: list[tuple[int, AnalysisResult]] = [(0, base_result)]
    seen_seam_sets: set[frozenset[int]] = set()
    seen_seam_sets.add(frozenset(base_result.seam_edges))
    previous_conservative_count = len(base_result.seam_edges)

    for order, (label, options, candidate_kind) in enumerate(
        candidate_specs, start=1
    ):
        result = analyze_mesh(mesh, options)
        if candidate_kind == "dense":
            original_edges = set(result.seam_edges)
            result.seam_edges.update(base_result.seam_edges)
            if result.seam_edges != original_edges:
                data = _normalize_mesh(mesh)
                positions_by_index = {
                    edge.result_index: edge.position for edge in data.edges
                }
                seam_positions = {
                    positions_by_index[index]
                    for index in result.seam_edges
                    if index in positions_by_index
                }
                result.chart_count = count_charts(
                    len(data.faces), data.edge_faces, seam_positions
                )

        seam_key = frozenset(result.seam_edges)
        seam_count = len(seam_key)

        # 보수 후보가 직전 보수 단계보다 Seam을 늘리면 후보 계약에서 제외한다.
        if (
            candidate_kind == "conservative"
            and seam_count > previous_conservative_count
        ):
            continue
        if seam_key in seen_seam_sets:
            continue

        result.options = options
        result.candidate_label = label
        candidates.append((order, result))
        seen_seam_sets.add(seam_key)
        if candidate_kind == "conservative":
            previous_conservative_count = seam_count

    candidates.sort(key=lambda item: (-len(item[1].seam_edges), item[0]))
    return [result for _, result in candidates]


__all__ = [
    "AnalysisOptions",
    "AnalysisPreset",
    "AnalysisResult",
    "analyze_mesh",
    "detect_analysis_preset",
    "generate_analysis_candidates",
    "resolve_auto_quality_level",
]
