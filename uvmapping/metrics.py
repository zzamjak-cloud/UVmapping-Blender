"""Seam 결과의 UV Chart 연결성 지표."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence


def face_chart_ids(
    face_count: int,
    edge_faces: Sequence[Sequence[int]],
    seam_edge_positions: Iterable[int],
) -> list[int]:
    """Seam을 통과하지 않는 Face 연결 요소 번호를 결정론적으로 반환한다."""

    if face_count <= 0:
        return []

    seam_positions = set(seam_edge_positions)
    adjacency: list[set[int]] = [set() for _ in range(face_count)]
    for edge_position, linked_faces in enumerate(edge_faces):
        if edge_position in seam_positions or len(linked_faces) != 2:
            continue
        first, second = linked_faces
        if first == second or not (0 <= first < face_count and 0 <= second < face_count):
            continue
        adjacency[first].add(second)
        adjacency[second].add(first)

    chart_ids = [-1] * face_count
    next_chart = 0
    for start in range(face_count):
        if chart_ids[start] != -1:
            continue
        chart_ids[start] = next_chart
        queue = deque([start])
        while queue:
            face = queue.popleft()
            for neighbor in sorted(adjacency[face]):
                if chart_ids[neighbor] == -1:
                    chart_ids[neighbor] = next_chart
                    queue.append(neighbor)
        next_chart += 1
    return chart_ids


def chart_face_counts(chart_ids: Sequence[int]) -> dict[int, int]:
    """Chart별 Face 수를 센다."""

    counts: dict[int, int] = {}
    for chart_id in chart_ids:
        counts[chart_id] = counts.get(chart_id, 0) + 1
    return counts


def count_charts(
    face_count: int,
    edge_faces: Sequence[Sequence[int]],
    seam_edge_positions: Iterable[int],
) -> int:
    """Seam 적용 뒤 생성되는 Face Chart 수를 반환한다."""

    ids = face_chart_ids(face_count, edge_faces, seam_edge_positions)
    return max(ids, default=-1) + 1


def face_components(
    face_count: int,
    edge_faces: Sequence[Sequence[int]],
) -> list[set[int]]:
    """Seam을 무시한 원래 메시의 Face 연결 요소를 반환한다."""

    chart_ids = face_chart_ids(face_count, edge_faces, ())
    components: dict[int, set[int]] = {}
    for face, component_id in enumerate(chart_ids):
        components.setdefault(component_id, set()).add(face)
    return [components[key] for key in sorted(components)]


def boundary_vertex_groups(
    component_faces: set[int],
    edge_vertices: Sequence[tuple[int, int]],
    edge_faces: Sequence[Sequence[int]],
) -> list[set[int]]:
    """한 Face 컴포넌트의 경계 루프/체인을 Vertex 그룹으로 근사한다."""

    adjacency: dict[int, set[int]] = {}
    for edge_position, linked_faces in enumerate(edge_faces):
        inside = sum(face in component_faces for face in linked_faces)
        if inside != 1:
            continue
        first, second = edge_vertices[edge_position]
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)

    groups: list[set[int]] = []
    remaining = set(adjacency)
    while remaining:
        start = min(remaining)
        group = {start}
        queue = deque([start])
        remaining.remove(start)
        while queue:
            vertex = queue.popleft()
            for neighbor in sorted(adjacency.get(vertex, ())):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    group.add(neighbor)
                    queue.append(neighbor)
        groups.append(group)
    return groups
