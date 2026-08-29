"""격리 Blender에서 자동 UV 결과의 기본 품질을 검사합니다."""

from __future__ import annotations

import bpy


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _signed_area(points) -> float:
    return 0.5 * sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:] + points[:1], strict=True)
    )


def _triangle_intersection_area(subject, clip) -> float:
    """Sutherland-Hodgman 방식으로 두 UV 삼각형의 교차 면적을 구합니다."""

    orientation = 1.0 if _signed_area(clip) >= 0.0 else -1.0
    polygon = list(subject)
    for clip_start, clip_end in zip(clip, clip[1:] + clip[:1], strict=True):
        if not polygon:
            return 0.0
        output = []

        def side(point) -> float:
            return orientation * (
                (clip_end[0] - clip_start[0]) * (point[1] - clip_start[1])
                - (clip_end[1] - clip_start[1]) * (point[0] - clip_start[0])
            )

        previous = polygon[-1]
        previous_side = side(previous)
        for current in polygon:
            current_side = side(current)
            previous_inside = previous_side >= -1.0e-12
            current_inside = current_side >= -1.0e-12
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
    return abs(_signed_area(polygon)) if len(polygon) >= 3 else 0.0


def _overlap_pair_count(mesh_object) -> int:
    mesh = mesh_object.data
    uv_layer = mesh.uv_layers.active
    triangles = []
    for polygon in mesh.polygons:
        coordinates = [tuple(uv_layer.data[index].uv) for index in polygon.loop_indices]
        for offset in range(1, len(coordinates) - 1):
            triangles.append((coordinates[0], coordinates[offset], coordinates[offset + 1]))

    overlaps = 0
    for index, first in enumerate(triangles):
        for second in triangles[index + 1 :]:
            if _triangle_intersection_area(first, second) > 1.0e-10:
                overlaps += 1
    return overlaps


def _check(name: str, create_mesh) -> None:
    create_mesh()
    mesh_object = bpy.context.object
    result = bpy.ops.uvmapping.auto_unwrap()
    assert result == {"FINISHED"}, f"{name}: 자동 언랩 실패: {result}"

    seam_count = sum(edge.use_seam for edge in mesh_object.data.edges)
    edge_count = len(mesh_object.data.edges)
    overlap_count = _overlap_pair_count(mesh_object)
    assert seam_count > 0, f"{name}: Seam이 생성되지 않았습니다."
    assert seam_count <= max(8, int(edge_count * 0.70)), (
        f"{name}: Seam 비율이 과도합니다: {seam_count}/{edge_count}"
    )
    assert overlap_count == 0, f"{name}: 겹친 UV 삼각형 쌍 {overlap_count}개"
    print(
        f"[quality] {name}: seams={seam_count}/{edge_count}, "
        f"overlap_pairs={overlap_count}"
    )
    bpy.data.objects.remove(mesh_object, do_unlink=True)


def main() -> None:
    _clear_scene()
    settings = bpy.context.scene.uvmapping_settings
    settings.preset = "BALANCED"
    settings.use_custom_analysis = False
    settings.seam_policy = "REPLACE"

    cases = (
        ("Cube", lambda: bpy.ops.mesh.primitive_cube_add(size=2.0)),
        (
            "Cylinder",
            lambda: bpy.ops.mesh.primitive_cylinder_add(
                vertices=24, radius=1.0, depth=2.0
            ),
        ),
        (
            "Sphere",
            lambda: bpy.ops.mesh.primitive_uv_sphere_add(
                segments=24, ring_count=12
            ),
        ),
        (
            "Torus",
            lambda: bpy.ops.mesh.primitive_torus_add(
                major_segments=16, minor_segments=6
            ),
        ),
    )
    for name, create_mesh in cases:
        _check(name, create_mesh)
    print("[quality] Seam 비율 및 UV overlap 검사 통과")


if __name__ == "__main__":
    main()
