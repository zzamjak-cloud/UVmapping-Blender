"""Blender 없이 실행하는 자동 Seam 분석 코어 회귀 테스트."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, radians, sin, tau
from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.analysis import (
    AnalysisOptions,
    AnalysisPreset,
    analyze_mesh,
    detect_analysis_preset,
    generate_analysis_candidates,
    resolve_auto_quality_level,
)


@dataclass
class _Vertex:
    index: int
    co: tuple[float, float, float]


@dataclass
class _Edge:
    index: int
    vertices: tuple[int, int]
    use_seam: bool = False
    use_edge_sharp: bool = False


@dataclass
class _Polygon:
    index: int
    vertices: tuple[int, ...]
    material_index: int = 0


@dataclass
class _Mesh:
    vertices: list[_Vertex]
    edges: list[_Edge]
    polygons: list[_Polygon]


def _mesh(
    coordinates: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    *,
    materials: list[int] | None = None,
    seams: set[tuple[int, int]] | None = None,
    sharp: set[tuple[int, int]] | None = None,
) -> _Mesh:
    edge_keys: dict[tuple[int, int], int] = {}
    for face in faces:
        for position, first in enumerate(face):
            second = face[(position + 1) % len(face)]
            edge_keys.setdefault(tuple(sorted((first, second))), len(edge_keys))
    seam_keys = {tuple(sorted(key)) for key in seams or set()}
    sharp_keys = {tuple(sorted(key)) for key in sharp or set()}
    ordered_edges = sorted(edge_keys, key=edge_keys.get)
    return _Mesh(
        vertices=[_Vertex(index, coordinate) for index, coordinate in enumerate(coordinates)],
        edges=[
            _Edge(
                index=index,
                vertices=key,
                use_seam=key in seam_keys,
                use_edge_sharp=key in sharp_keys,
            )
            for index, key in enumerate(ordered_edges)
        ],
        polygons=[
            _Polygon(index, face, (materials or [0] * len(faces))[index])
            for index, face in enumerate(faces)
        ],
    )


def _linked_face_counts(mesh: _Mesh) -> dict[int, int]:
    counts = {edge.index: 0 for edge in mesh.edges}
    by_key = {tuple(sorted(edge.vertices)): edge.index for edge in mesh.edges}
    for polygon in mesh.polygons:
        for position, first in enumerate(polygon.vertices):
            second = polygon.vertices[(position + 1) % len(polygon.vertices)]
            counts[by_key[tuple(sorted((first, second)))]] += 1
    return counts


def test_open_quad_marks_boundaries_without_mutating_mesh() -> None:
    mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [(0, 1, 2, 3)],
    )

    result = analyze_mesh(mesh)

    assert result.seam_edges == {0, 1, 2, 3}
    assert result.chart_count == 1
    assert all(not edge.use_seam for edge in mesh.edges)


def test_closed_smooth_component_gets_deterministic_cut_path() -> None:
    mesh = _mesh(
        [(1, 1, 1), (-1, -1, 1), (-1, 1, -1), (1, -1, -1)],
        [(0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)],
    )
    options = AnalysisOptions(
        angle_weight=0.0,
        length_weight=0.0,
        sharp_weight=0.0,
        material_weight=0.0,
        existing_seam_weight=0.0,
        seam_threshold=1.0,
        min_chart_faces=1,
        split_curved_charts=False,
    )

    first = analyze_mesh(mesh, options)
    second = analyze_mesh(mesh, options)

    assert first.seam_edges
    assert first.seam_edges == second.seam_edges
    assert first.edge_scores == second.edge_scores
    assert any("닫힌 컴포넌트" in warning for warning in first.warnings)


def _uv_sphere(segments: int = 16, rings: int = 8) -> _Mesh:
    """자잘한 이면각 때문에 점수 기반 Seam이 생기지 않는 매끈한 구."""

    coordinates: list[tuple[float, float, float]] = [(0.0, 0.0, 1.0)]
    for ring in range(1, rings):
        phi = (tau / 2.0) * ring / rings
        for segment in range(segments):
            theta = tau * segment / segments
            coordinates.append(
                (sin(phi) * cos(theta), sin(phi) * sin(theta), cos(phi))
            )
    coordinates.append((0.0, 0.0, -1.0))

    def ring_vertex(ring: int, segment: int) -> int:
        return 1 + (ring - 1) * segments + (segment % segments)

    faces: list[tuple[int, ...]] = []
    for segment in range(segments):
        faces.append((0, ring_vertex(1, segment), ring_vertex(1, segment + 1)))
    for ring in range(1, rings - 1):
        for segment in range(segments):
            faces.append(
                (
                    ring_vertex(ring, segment),
                    ring_vertex(ring + 1, segment),
                    ring_vertex(ring + 1, segment + 1),
                    ring_vertex(ring, segment + 1),
                )
            )
    bottom = len(coordinates) - 1
    for segment in range(segments):
        faces.append(
            (ring_vertex(rings - 1, segment + 1), ring_vertex(rings - 1, segment), bottom)
        )
    return _mesh(coordinates, faces)


def test_smooth_closed_mesh_is_split_by_curvature_clusters() -> None:
    mesh = _uv_sphere()

    first = analyze_mesh(mesh)
    second = analyze_mesh(mesh)

    assert first.seam_edges
    assert first.chart_count >= 2
    assert first.seam_edges == second.seam_edges
    assert any("누적 곡률" in warning for warning in first.warnings)
    # 곡률 분할이 지름 절단 경로보다 먼저 적용되어 중복 절단이 없어야 한다.
    assert all("닫힌 컴포넌트" not in warning for warning in first.warnings)


def test_curvature_split_can_be_disabled() -> None:
    mesh = _uv_sphere()

    result = analyze_mesh(mesh, AnalysisOptions(split_curved_charts=False, min_chart_faces=1))

    assert result.chart_count == 1
    assert any("닫힌 컴포넌트" in warning for warning in result.warnings)


def test_developable_tube_is_not_split_by_curvature() -> None:
    result = analyze_mesh(_tube(), AnalysisOptions(min_chart_faces=1))

    # 원기둥 옆면은 각결손이 0이므로 곡률 분할이 개입하면 안 된다.
    assert all("누적 곡률" not in warning for warning in result.warnings)


def _tube(segments: int = 12, rings: int = 4, sharp: set[tuple[int, int]] | None = None) -> _Mesh:
    coordinates = [
        (cos(tau * segment / segments), sin(tau * segment / segments), float(ring))
        for ring in range(rings)
        for segment in range(segments)
    ]
    faces = [
        (
            ring * segments + segment,
            ring * segments + (segment + 1) % segments,
            (ring + 1) * segments + (segment + 1) % segments,
            (ring + 1) * segments + segment,
        )
        for ring in range(rings - 1)
        for segment in range(segments)
    ]
    return _mesh(coordinates, faces, sharp=sharp)


def test_auto_preset_detects_hard_surface_for_sharp_cube() -> None:
    mesh = _mesh(
        [
            (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
            (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1),
        ],
        [
            (0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
            (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0),
        ],
    )

    assert detect_analysis_preset(mesh) == AnalysisPreset.HARD_SURFACE


def test_auto_preset_detects_organic_for_smooth_sphere() -> None:
    assert detect_analysis_preset(_uv_sphere()) == AnalysisPreset.ORGANIC


def test_auto_preset_detects_balanced_for_mixed_mesh() -> None:
    # 완만한 튜브의 내부 링 엣지에 Sharp 몇 개만 섞으면 피처 비율이 중간 구간에 온다.
    sharp = {(12, 13), (13, 14), (14, 15), (15, 16)}
    mesh = _tube(sharp=sharp)

    assert detect_analysis_preset(mesh) == AnalysisPreset.BALANCED


def test_auto_quality_level_scales_with_face_count() -> None:
    assert resolve_auto_quality_level(100) == "QUALITY"
    assert resolve_auto_quality_level(4000) == "QUALITY"
    assert resolve_auto_quality_level(4001) == "BALANCED"
    assert resolve_auto_quality_level(30000) == "BALANCED"
    assert resolve_auto_quality_level(30001) == "FAST"


def test_material_boundary_is_selected_for_hard_surface() -> None:
    mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)],
        [(0, 1, 2, 3), (1, 4, 5, 2)],
        materials=[0, 1],
    )
    shared_index = next(
        edge.index for edge in mesh.edges if set(edge.vertices) == {1, 2}
    )

    result = analyze_mesh(
        mesh,
        AnalysisOptions.for_preset("hard surface", min_chart_faces=1),
    )

    assert shared_index in result.seam_edges
    assert result.chart_count == 2


def test_multiple_boundary_loops_are_connected_by_internal_cut() -> None:
    mesh = _mesh(
        [
            (-2, -2, 0),
            (2, -2, 0),
            (2, 2, 0),
            (-2, 2, 0),
            (-1, -1, 0),
            (1, -1, 0),
            (1, 1, 0),
            (-1, 1, 0),
        ],
        [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
    )
    linked_counts = _linked_face_counts(mesh)

    result = analyze_mesh(mesh, AnalysisOptions(min_chart_faces=1))

    assert any(
        edge_index in result.seam_edges and linked_counts[edge_index] == 2
        for edge_index in linked_counts
    )
    assert any("경계 2개" in warning for warning in result.warnings)


def test_small_material_chart_is_merged_when_allowed() -> None:
    mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)],
        [(0, 1, 2, 3), (1, 4, 5, 2)],
        materials=[0, 1],
    )
    result = analyze_mesh(
        mesh,
        AnalysisOptions.for_preset("HARD_SURFACE", min_chart_faces=2),
    )

    assert result.chart_count == 1
    assert any("작은 UV Island" in warning for warning in result.warnings)


def test_existing_seam_is_preserved_even_for_small_chart() -> None:
    mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)],
        [(0, 1, 2, 3), (1, 4, 5, 2)],
        seams={(1, 2)},
    )
    shared_index = next(
        edge.index for edge in mesh.edges if set(edge.vertices) == {1, 2}
    )

    result = analyze_mesh(mesh, AnalysisOptions(min_chart_faces=2))

    assert shared_index in result.seam_edges
    assert result.chart_count == 2


def _punctured_torus() -> _Mesh:
    major_segments = 4
    minor_segments = 7
    removed_face = 1
    coordinates = []
    for major in range(major_segments):
        u = tau * major / major_segments
        for minor in range(minor_segments):
            v = tau * minor / minor_segments
            radius = 2.0 + 0.6 * cos(v)
            coordinates.append((radius * cos(u), radius * sin(u), 0.6 * sin(v)))

    faces = []
    for major in range(major_segments):
        for minor in range(minor_segments):
            if major * minor_segments + minor == removed_face:
                continue
            faces.append(
                (
                    major * minor_segments + minor,
                    ((major + 1) % major_segments) * minor_segments + minor,
                    ((major + 1) % major_segments) * minor_segments
                    + (minor + 1) % minor_segments,
                    major * minor_segments + (minor + 1) % minor_segments,
                )
            )
    return _mesh(coordinates, faces)


def _cut_euler_characteristic(mesh: _Mesh, seams: set[int]) -> int:
    edge_by_key = {tuple(sorted(edge.vertices)): edge.index for edge in mesh.edges}
    edge_faces = [[] for _ in mesh.edges]
    for face_index, polygon in enumerate(mesh.polygons):
        for offset, first in enumerate(polygon.vertices):
            second = polygon.vertices[(offset + 1) % len(polygon.vertices)]
            edge_faces[edge_by_key[tuple(sorted((first, second)))]].append(face_index)

    parent = {
        (face_index, vertex): (face_index, vertex)
        for face_index, polygon in enumerate(mesh.polygons)
        for vertex in polygon.vertices
    }

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(first, second) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    split_edge_count = 0
    for edge in mesh.edges:
        linked_faces = edge_faces[edge.index]
        if edge.index not in seams and len(linked_faces) == 2:
            split_edge_count += 1
            for vertex in edge.vertices:
                union((linked_faces[0], vertex), (linked_faces[1], vertex))
        else:
            split_edge_count += len(linked_faces)

    split_vertex_count = len({find(corner) for corner in parent})
    return split_vertex_count - split_edge_count + len(mesh.polygons)


def test_punctured_torus_handle_cut_is_a_disk() -> None:
    mesh = _punctured_torus()
    options = AnalysisOptions(
        angle_weight=0.0,
        length_weight=0.0,
        sharp_weight=0.0,
        material_weight=0.0,
        existing_seam_weight=0.0,
        boundary_weight=0.0,
        non_manifold_weight=0.0,
        seam_threshold=1.0,
        min_chart_faces=1,
        split_curved_charts=False,
        preserve_existing_seams=False,
    )
    result = analyze_mesh(mesh, options)

    assert result.chart_count == 1
    assert _cut_euler_characteristic(mesh, result.seam_edges) == 1


def test_fast_analysis_candidate_returns_only_base_result() -> None:
    mesh = _punctured_torus()
    options = AnalysisOptions(min_chart_faces=1)

    candidates = generate_analysis_candidates(mesh, options, "FAST")

    assert len(candidates) == 1
    assert candidates[0].seam_edges == analyze_mesh(mesh, options).seam_edges
    assert candidates[0].options == options
    assert candidates[0].candidate_label == "기본"


def test_analysis_candidates_are_monotonic_and_deterministic() -> None:
    mesh = _punctured_torus()
    options = AnalysisOptions(min_chart_faces=1)

    first = generate_analysis_candidates(mesh, options, "QUALITY")
    second = generate_analysis_candidates(mesh, options, "QUALITY")

    assert [candidate.seam_edges for candidate in first] == [
        candidate.seam_edges for candidate in second
    ]
    assert [candidate.candidate_label for candidate in first] == [
        candidate.candidate_label for candidate in second
    ]
    seam_counts = [len(candidate.seam_edges) for candidate in first]
    assert seam_counts == sorted(seam_counts, reverse=True)
    assert len({frozenset(candidate.seam_edges) for candidate in first}) == len(first)
    assert all(candidate.options is not None for candidate in first)


def test_quality_candidates_include_dense_safe_candidate() -> None:
    angle = radians(15.0)
    mesh = _mesh(
        [
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, -cos(angle), sin(angle)),
        ],
        [(0, 1, 2), (1, 0, 3)],
    )
    options = AnalysisOptions(min_chart_faces=1)

    first = generate_analysis_candidates(mesh, options, "QUALITY")
    second = generate_analysis_candidates(mesh, options, "QUALITY")
    by_label = {candidate.candidate_label: candidate for candidate in first}

    assert len(first) <= 5
    assert "기본" in by_label
    assert "조밀 안전" in by_label
    assert by_label["조밀 안전"].options is not None
    assert by_label["조밀 안전"].options.seam_threshold < options.seam_threshold
    assert by_label["조밀 안전"].seam_edges > by_label["기본"].seam_edges
    assert [candidate.seam_edges for candidate in first] == [
        candidate.seam_edges for candidate in second
    ]
    assert [len(candidate.seam_edges) for candidate in first] == sorted(
        (len(candidate.seam_edges) for candidate in first), reverse=True
    )


def test_analysis_candidates_preserve_topology_cut_graph() -> None:
    mesh = _punctured_torus()
    options = AnalysisOptions(
        angle_weight=0.0,
        length_weight=0.0,
        sharp_weight=0.0,
        material_weight=0.0,
        existing_seam_weight=0.0,
        boundary_weight=0.0,
        non_manifold_weight=0.0,
        seam_threshold=1.0,
        min_chart_faces=1,
        split_curved_charts=False,
        preserve_existing_seams=False,
    )
    topology_edges = analyze_mesh(mesh, options).seam_edges

    candidates = generate_analysis_candidates(mesh, options, "QUALITY")

    assert all(
        topology_edges <= candidate.seam_edges
        for candidate in candidates
    )
    assert all(
        _cut_euler_characteristic(mesh, candidate.seam_edges) == 1
        for candidate in candidates
    )


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 분석 테스트 {len(tests)}/{len(tests)} 통과")
