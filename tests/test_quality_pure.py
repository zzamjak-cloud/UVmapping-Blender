"""Blender 없이 실행하는 UV 품질 및 텍스처 계약 회귀 테스트."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.contracts import build_texture_job
from uvmapping.quality import (
    OVERLAP_BUDGET_EXCEEDED,
    OVERLAP_EXACT,
    _EPSILON,
    _extract_faces,
    _signed_uv_area,
    _triangle_intersection_area,
    _triangulate,
    evaluate_uv_quality,
)


@dataclass
class _Vertex:
    index: int
    co: tuple[float, float, float]


@dataclass
class _Edge:
    index: int
    vertices: tuple[int, int]


@dataclass
class _Polygon:
    index: int
    vertices: tuple[int, ...]
    loop_indices: tuple[int, ...]
    material_index: int = 0


@dataclass
class _UVLoop:
    uv: tuple[float, float]


@dataclass
class _UVLayer:
    name: str
    data: list[_UVLoop]


class _UVLayers(list[_UVLayer]):
    @property
    def active(self) -> _UVLayer | None:
        return self[0] if self else None

    def get(self, name: str) -> _UVLayer | None:
        return next((layer for layer in self if layer.name == name), None)


@dataclass
class _Mesh:
    vertices: list[_Vertex]
    edges: list[_Edge]
    polygons: list[_Polygon]
    uv_layers: _UVLayers


def _mesh(
    coordinates: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    face_uvs: list[tuple[tuple[float, float], ...]],
) -> _Mesh:
    edge_indices: dict[tuple[int, int], int] = {}
    polygons: list[_Polygon] = []
    uv_loops: list[_UVLoop] = []
    for face_index, (face, uvs) in enumerate(zip(faces, face_uvs, strict=True)):
        assert len(face) == len(uvs)
        loop_start = len(uv_loops)
        uv_loops.extend(_UVLoop(uv) for uv in uvs)
        polygons.append(
            _Polygon(
                index=face_index,
                vertices=face,
                loop_indices=tuple(range(loop_start, loop_start + len(face))),
            )
        )
        for offset, first in enumerate(face):
            second = face[(offset + 1) % len(face)]
            key = (min(first, second), max(first, second))
            edge_indices.setdefault(key, len(edge_indices))
    ordered_edges = sorted(edge_indices, key=edge_indices.__getitem__)
    return _Mesh(
        vertices=[
            _Vertex(index=index, co=coordinate)
            for index, coordinate in enumerate(coordinates)
        ],
        edges=[
            _Edge(index=index, vertices=vertices)
            for index, vertices in enumerate(ordered_edges)
        ],
        polygons=polygons,
        uv_layers=_UVLayers([_UVLayer(name="AutoUV", data=uv_loops)]),
    )


def test_normal_atlas_is_valid_and_uses_deterministic_triangulation() -> None:
    mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [(0, 1, 2, 3)],
        [((0, 0), (1, 0), (1, 1), (0, 1))],
    )

    report = evaluate_uv_quality(mesh, "AutoUV", seam_count=0, chart_count=1)

    assert report.triangle_count == 2
    assert report.overlap_pairs == 0
    assert report.overlap_status == OVERLAP_EXACT
    assert report.degenerate_triangles == 0
    assert report.flipped_triangles == 0
    assert report.area_distortion_mean == 0.0
    assert report.area_distortion_p95 == 0.0
    assert report.utilization == 1.0
    assert report.chart_count == 1
    assert report.valid
    assert report.objective_score == 1.0
    assert report.uv_bounds == ((0.0, 0.0), (1.0, 1.0))


def test_concave_ngon_avoids_fan_area_overlap_and_flip_errors() -> None:
    coordinates = [
        (0.0, 0.0, 0.0),
        (3.0, 0.0, 0.0),
        (3.0, 3.0, 0.0),
        (2.0, 3.0, 0.0),
        (2.0, 1.0, 0.0),
        (1.0, 1.0, 0.0),
        (1.0, 3.0, 0.0),
        (0.0, 3.0, 0.0),
    ]
    uvs = tuple((coordinate[0], coordinate[1]) for coordinate in coordinates)
    mesh = _mesh(coordinates, [tuple(range(8))], [uvs])

    report = evaluate_uv_quality(mesh, chart_count=1)
    job = build_texture_job(mesh, "Concave", "AutoUV", report, [])

    assert report.triangle_count == 6
    assert report.overlap_pairs == 0
    assert report.flipped_triangles == 0
    assert report.area_distortion_mean == 0.0
    assert abs(report.utilization - 7.0 / 9.0) < 1.0e-12
    assert report.valid
    assert abs(job.texel_density["global"] - 1.0) < 1.0e-12
    assert job.islands[0]["bounds"] == ((0.0, 0.0), (3.0, 3.0))


def test_positive_area_overlap_is_counted_but_shared_edges_are_not() -> None:
    mesh = _mesh(
        [
            (0, 0, 0),
            (1, 0, 0),
            (0, 1, 0),
            (2, 0, 0),
            (3, 0, 0),
            (2, 1, 0),
        ],
        [(0, 1, 2), (3, 4, 5)],
        [((0, 0), (1, 0), (0, 1)), ((0, 0), (1, 0), (0, 1))],
    )

    report = evaluate_uv_quality(mesh)

    assert report.overlap_pairs == 1
    assert report.overlap_status == OVERLAP_EXACT
    assert not report.valid
    assert report.objective_score < 1.0


def test_degenerate_triangle_and_whole_mirrored_island_are_handled() -> None:
    degenerate_mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
        [(0, 1, 2)],
        [((0, 0), (0.5, 0), (1, 0))],
    )
    mirrored_mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [(0, 1, 2, 3)],
        [((0, 0), (0, 1), (1, 1), (1, 0))],
    )

    degenerate = evaluate_uv_quality(degenerate_mesh)
    mirrored = evaluate_uv_quality(mirrored_mesh)

    assert degenerate.degenerate_triangles == 1
    assert not degenerate.valid
    assert mirrored.flipped_triangles == 0
    assert mirrored.valid


def test_minority_orientation_inside_island_is_flipped() -> None:
    mesh = _mesh(
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
        [(0, 1, 2), (1, 3, 2)],
        [
            ((0, 0), (1, 0), (0, 1)),
            ((1, 0), (0, 0), (0, 1)),
        ],
    )

    report = evaluate_uv_quality(mesh)

    assert report.flipped_triangles == 1
    assert not report.valid


def _brute_force_overlap_count(mesh: _Mesh) -> int:
    _, faces, _ = _extract_faces(mesh)
    triangles = _triangulate(faces)
    overlaps = 0
    for first_index, first in enumerate(triangles):
        if abs(_signed_uv_area(first.uvs)) <= _EPSILON:
            continue
        for second in triangles[first_index + 1 :]:
            if abs(_signed_uv_area(second.uvs)) <= _EPSILON:
                continue
            if _triangle_intersection_area(first.uvs, second.uvs) > 1.0e-10:
                overlaps += 1
    return overlaps


def test_sweep_line_matches_small_brute_force_result() -> None:
    coordinates = []
    faces = []
    face_uvs = []
    atlas = (
        ((0, 0), (1, 0), (0, 1)),
        ((0.2, 0.2), (1.2, 0.2), (0.2, 1.2)),
        ((2, 0), (3, 0), (2, 1)),
        ((3, 0), (4, 0), (3, 1)),
        ((0.5, -0.5), (0.5, 1.5), (1.5, 0.5)),
    )
    for index, uvs in enumerate(atlas):
        vertex_start = len(coordinates)
        coordinates.extend(
            ((index * 2, 0, 0), (index * 2 + 1, 0, 0), (index * 2, 1, 0))
        )
        faces.append((vertex_start, vertex_start + 1, vertex_start + 2))
        face_uvs.append(uvs)
    mesh = _mesh(coordinates, faces, face_uvs)

    report = evaluate_uv_quality(mesh, max_pair_checks=None)

    assert report.overlap_status == OVERLAP_EXACT
    assert report.overlap_pairs == _brute_force_overlap_count(mesh)


def test_pathological_overlap_stops_at_pair_budget_without_duplicates() -> None:
    coordinates = []
    faces = []
    face_uvs = []
    triangle_count = 20
    for index in range(triangle_count):
        vertex_start = len(coordinates)
        coordinates.extend(
            ((index * 2, 0, 0), (index * 2 + 1, 0, 0), (index * 2, 1, 0))
        )
        faces.append((vertex_start, vertex_start + 1, vertex_start + 2))
        face_uvs.append(((0, 0), (1, 0), (0, 1)))
    mesh = _mesh(coordinates, faces, face_uvs)

    budgeted = evaluate_uv_quality(mesh, max_pair_checks=7)
    exact = evaluate_uv_quality(mesh, max_pair_checks=None)

    assert budgeted.overlap_status == OVERLAP_BUDGET_EXCEEDED
    assert budgeted.overlap_pairs == 7
    assert not budgeted.valid
    assert exact.overlap_status == OVERLAP_EXACT
    assert exact.overlap_pairs == triangle_count * (triangle_count - 1) // 2


def _two_quad_mesh(right_uv: float = 1.0) -> _Mesh:
    return _mesh(
        [
            (0, 0, 0),
            (1, 0, 0),
            (1, 1, 0),
            (0, 1, 0),
            (2, 0, 0),
            (2, 1, 0),
        ],
        [(0, 1, 2, 3), (1, 4, 5, 2)],
        [
            ((0, 0), (0.5, 0), (0.5, 1), (0, 1)),
            ((0.5, 0), (right_uv, 0), (right_uv, 1), (0.5, 1)),
        ],
    )


def test_texture_job_hash_and_islands_are_deterministic() -> None:
    mesh = _two_quad_mesh()
    shared_edge = next(
        edge.index for edge in mesh.edges if set(edge.vertices) == {1, 2}
    )
    report = evaluate_uv_quality(
        mesh,
        "AutoUV",
        seam_count=1,
        chart_count=2,
    )

    first = build_texture_job(mesh, "Panel", "AutoUV", report, [shared_edge])
    second = build_texture_job(mesh, "다른 오브젝트명", "AutoUV", report, [shared_edge])
    changed_mesh = _two_quad_mesh(right_uv=0.9)
    changed_report = evaluate_uv_quality(changed_mesh, "AutoUV")
    changed = build_texture_job(
        changed_mesh,
        "Panel",
        "AutoUV",
        changed_report,
        [shared_edge],
    )

    assert first.mesh_hash == second.mesh_hash
    assert first.mesh_hash != changed.mesh_hash
    assert first.topology_hash == changed.topology_hash
    assert first.geometry_hash == changed.geometry_hash
    assert first.uv_hash != changed.uv_hash
    assert first.job_id != second.job_id
    assert len(first.settings_hash) == 64
    assert first.face_to_island == {0: 0, 1: 1}
    assert first.island_adjacency == ((0, 1),)
    assert first.seam_edges == (shared_edge,)
    assert first.texel_density["global"] > 0.0
    assert first.texel_density["unit"] == "uv_per_object_unit"
    assert first.overlap_status == report.overlap_status
    assert first.addon_version == "1.0.0"
    assert first.resolution == (2048, 2048)
    assert first.padding == 16
    assert first.udim_tiles == (1001,)
    assert first.target_resolution == first.resolution
    assert first.requested_padding == first.padding
    assert first.requested_udim_tiles == first.udim_tiles
    assert first.packing_margin_method is None
    assert first.packing_margin_uv is None
    assert first.coordinate_space == "OBJECT"
    assert first.material_face_mapping == {0: 0, 1: 0}
    assert first.islands[0]["faces"] == (0,)
    assert first.islands[0]["adjacent_islands"] == (1,)
    assert first.guide_map_manifest["maps"]["normal"]["status"] == (
        "NOT_GENERATED"
    )
    assert json.loads(json.dumps(first.to_dict(), ensure_ascii=False))["mesh_hash"] == (
        first.mesh_hash
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
    print(f"순수 UV 품질 테스트 {len(tests)}/{len(tests)} 통과")
