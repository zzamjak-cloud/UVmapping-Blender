"""Blender 없이 실행하는 CPU Diffuse/Albedo Atlas 회귀 테스트."""

from __future__ import annotations

from pathlib import Path
import struct
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_bake import (
    ALIGNMENT_MIN_IOU,
    SILHOUETTE_HOLE_LIMIT,
    SILHOUETTE_SEGMENT_MISMATCH_LIMIT,
    VIEW_GAIN_RANGE,
    background_flood_mask,
    coarse_foreground_mask,
    compare_rendered_view,
    compose_verification_sheet,
    evaluate_silhouette_match,
    silhouette_match_passed,
    BakeTriangle,
    VIEW_NAMES,
    VIEW_SPECS,
    build_raster_source,
    dilate_mask,
    estimate_view_gains,
    extend_foreground,
    _estimate_view_gains,
    _generated_topology,
    _normal_view_weights,
    _outside_atlas,
    _projected_bbox,
    _resolve_view_paths,
    _visibility_weight,
    RasterSource,
    align_silhouette,
    prepare_sources,
    _linear_to_srgb_byte,
    _srgb_to_linear,
    barycentric_weights,
    bilinear_sample,
    build_depth_buffer,
    detect_foreground_bbox,
    dilate_rgba,
    encode_srgb_png,
    project_point,
    rasterize_atlas,
)


def _solid_source(color: tuple[float, float, float, float]) -> RasterSource:
    return build_raster_source(
        width=4,
        height=4,
        pixels=list(color * 16),
        subject_bbox=(0, 0, 4, 4),
        background=(1.0, 1.0, 1.0, 1.0),
        background_threshold=0.05,
        fallback_color=color,
    )


def test_projection_axes_and_near_depth_are_consistent() -> None:
    center = (0.0, 0.0, 0.0)
    assert project_point((0.25, -0.5, 0.25), "FRONT", center, 1.0) == (0.75, 0.75, 0.5)
    assert project_point((0.5, 0.25, 0.25), "RIGHT", center, 1.0) == (0.75, 0.75, 0.5)
    assert project_point((-0.25, 0.5, 0.25), "BACK", center, 1.0) == (0.75, 0.75, 0.5)
    assert project_point((-0.5, -0.25, 0.25), "LEFT", center, 1.0) == (0.75, 0.75, 0.5)
    # TOP은 위에서 내려다본 numpad 7 배치(+X 오른쪽, +Y 위, +Z가 가까움).
    assert project_point((0.25, 0.25, 0.5), "TOP", center, 1.0) == (0.75, 0.75, 0.5)
    # BOTTOM은 아래에서 올려다본 배치(+X 오른쪽, -Y 위, -Z가 가까움).
    assert project_point((0.25, -0.25, -0.5), "BOTTOM", center, 1.0) == (0.75, 0.75, 0.5)
    assert VIEW_NAMES == ("FRONT", "RIGHT", "BACK", "LEFT", "TOP", "BOTTOM")


def test_view_specs_form_right_handed_frames() -> None:
    """뷰를 추가할 때 손 방향이 뒤집히면 렌더와 좌우가 어긋난다. right × up == toward_camera여야 한다."""

    for name, spec in VIEW_SPECS.items():
        rx, ry, rz = spec.right
        ux, uy, uz = spec.up
        cross = (ry * uz - rz * uy, rz * ux - rx * uz, rx * uy - ry * ux)
        assert cross == spec.toward_camera, (name, cross, spec.toward_camera)
        assert spec.name == name


def test_visibility_weight_is_soft_and_monotonic() -> None:
    resolution = 4
    depth_buffer = [0.5] * (resolution * resolution)
    tolerance = 0.0625
    weights = [
        _visibility_weight((0.5, 0.5, 0.5 - gap), depth_buffer, resolution, tolerance)
        for gap in (0.0, 0.0625, 0.125, 0.1875, 0.25, 0.3125)
    ]
    assert weights[0] == weights[1] == 1.0
    assert all(later <= earlier for earlier, later in zip(weights, weights[1:]))
    assert 0.0 < weights[3] < weights[2] < 1.0
    assert weights[4] == 0.0 and weights[5] == 0.0
    # 화면 밖과 아무것도 그려지지 않은 픽셀은 보이지 않는다.
    assert _visibility_weight((1.5, 0.5, 0.5), depth_buffer, resolution, tolerance) == 0.0
    assert _visibility_weight((0.5, 0.5, 0.5), [float("-inf")] * 16, resolution, tolerance) == 0.0


def test_vertex_normals_blend_views_per_pixel() -> None:
    """정점 법선이 FRONT에서 RIGHT로 돌아가는 면은 픽셀마다 두 뷰가 섞여야 한다."""

    front_normal = (0.0, -1.0, 0.0)
    right_normal = (1.0, 0.0, 0.0)
    # 두 뷰 모두에서 면적이 보이도록 45도로 세운 면.
    triangle = BakeTriangle(
        positions=((-0.3, -0.3, -0.4), (0.3, 0.3, -0.4), (0.3, 0.3, 0.4)),
        uvs=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
        normal=front_normal,
        vertex_normals=(front_normal, right_normal, right_normal),
    )
    sources = {
        "FRONT": _solid_source((0.8, 0.1, 0.1, 1.0)),
        "RIGHT": _solid_source((0.1, 0.8, 0.1, 1.0)),
        "BACK": _solid_source((0.1, 0.1, 0.8, 1.0)),
    }
    rgba, _metrics = rasterize_atlas(
        (triangle,), sources, 32, 0, (0.0, 0.0, 0.0), 1.0, harmonize_colors=False
    )

    def red_minus_green(x: int, y: int) -> int:
        offset = (y * 32 + x) * 4
        return rgba[offset] - rgba[offset + 1]

    near_front = red_minus_green(2, 0)
    middle = red_minus_green(16, 8)
    near_right = red_minus_green(30, 24)
    assert near_front > 0 > near_right
    assert near_front > middle > near_right
    # 면 법선만 있으면 삼각형 전체가 FRONT 색 하나로 칠해진다.
    flat = BakeTriangle(positions=triangle.positions, uvs=triangle.uvs, normal=front_normal)
    flat_rgba, _ = rasterize_atlas(
        (flat,), sources, 32, 0, (0.0, 0.0, 0.0), 1.0, harmonize_colors=False
    )
    assert flat_rgba[(24 * 32 + 30) * 4] == flat_rgba[(0 * 32 + 2) * 4]


def test_blend_exponent_narrows_transition() -> None:
    normal = (0.6, -0.8, 0.0)
    soft = _normal_view_weights(normal, 2.0)
    sharp = _normal_view_weights(normal, 6.0)
    assert soft["FRONT"] > soft["RIGHT"] > 0.0
    assert soft["BACK"] == soft["LEFT"] == soft["TOP"] == soft["BOTTOM"] == 0.0
    assert sharp["RIGHT"] / sharp["FRONT"] < soft["RIGHT"] / soft["FRONT"]


def _slanted_triangles(count: int) -> tuple[BakeTriangle, ...]:
    """FRONT와 RIGHT가 함께 보는 45도 면을 여러 장 만든다."""

    normal = (0.7071067811865476, -0.7071067811865476, 0.0)
    triangles = []
    for index in range(count):
        z = -0.4 + 0.8 * index / count
        height = 0.8 / count
        triangles.append(
            BakeTriangle(
                positions=((-0.3, -0.3, z), (0.3, 0.3, z), (0.3, 0.3, z + height)),
                uvs=((0.1, 0.1), (0.9, 0.1), (0.9, 0.9)),
                normal=normal,
            )
        )
    return tuple(triangles)


def _gain_inputs(triangles, sources):
    center, scale = (0.0, 0.0, 0.0), 1.0
    views = [name for name in VIEW_NAMES if name in sources or name == "LEFT"]
    bboxes = {name: _projected_bbox(triangles, name, center, scale) for name in views}
    depth = {name: build_depth_buffer(triangles, name, center, scale, 64) for name in views}
    return center, scale, depth, 64, scale / 64 * 3.0, bboxes


def test_view_gains_follow_front_and_are_clamped() -> None:
    triangles = _slanted_triangles(12)
    front = (0.4, 0.4, 0.4, 1.0)
    sources = {
        "FRONT": _solid_source(front),
        "RIGHT": _solid_source((0.45, 0.45, 0.45, 1.0)),
        "BACK": _solid_source((0.1, 0.1, 0.8, 1.0)),
    }
    gains, pair_counts = _estimate_view_gains(triangles, sources, *_gain_inputs(triangles, sources))
    assert gains == estimate_view_gains(triangles, sources, *_gain_inputs(triangles, sources))
    assert gains["FRONT"] == (1.0, 1.0, 1.0)
    # 휘도 스칼라 gain 하나를 세 채널에 같이 쓴다.
    assert all(abs(value - 0.4 / 0.45) < 1.0e-6 for value in gains["RIGHT"])
    assert pair_counts[("FRONT", "RIGHT")] >= 32
    # FRONT와 겹쳐 보는 면이 없는 BACK은 표본 쌍 자체가 없어서 보정하지 않는다.
    assert gains["BACK"] == (1.0, 1.0, 1.0)
    assert ("FRONT", "BACK") not in pair_counts and ("BACK", "FRONT") not in pair_counts
    # LEFT는 RIGHT 이미지를 빌려 쓰므로 같은 이미지에 두 가지 보정이 걸리지 않게 gain을 물려받는다.
    assert gains["LEFT"] == gains["RIGHT"]
    assert not any("LEFT" in pair for pair in pair_counts)

    sources["RIGHT"] = _solid_source((0.8, 0.8, 0.8, 1.0))
    clamped = estimate_view_gains(triangles, sources, *_gain_inputs(triangles, sources))
    assert all(abs(value - VIEW_GAIN_RANGE[0]) < 1.0e-6 for value in clamped["RIGHT"])

    # 표본 쌍이 있어도 32개 미만이면 보정하지 않는다(겹침 없음과는 다른 이유).
    few = _slanted_triangles(4)
    sparse, sparse_counts = _estimate_view_gains(few, sources, *_gain_inputs(few, sources))
    assert 0 < sparse_counts[("FRONT", "RIGHT")] < 32
    assert sparse["RIGHT"] == (1.0, 1.0, 1.0)


def test_rasterizer_applies_view_gains() -> None:
    triangles = _slanted_triangles(12)
    sources = {
        "FRONT": _solid_source((0.4, 0.4, 0.4, 1.0)),
        "RIGHT": _solid_source((0.45, 0.45, 0.45, 1.0)),
        "BACK": _solid_source((0.1, 0.1, 0.8, 1.0)),
    }
    _plain, plain_metrics = rasterize_atlas(
        triangles, sources, 32, 0, (0.0, 0.0, 0.0), 1.0, harmonize_colors=False
    )
    harmonized, metrics = rasterize_atlas(triangles, sources, 32, 0, (0.0, 0.0, 0.0), 1.0)
    assert plain_metrics["view_gains"]["RIGHT"] == [1.0, 1.0, 1.0]
    assert metrics["view_gains"]["RIGHT"] == [round(0.4 / 0.45, 4)] * 3
    # 보정 뒤 45도 면은 FRONT의 0.4 회색에 수렴한다.
    center = (16 * 32 + 20) * 4
    assert harmonized[center] == _linear_to_srgb_byte(0.4)


def test_top_source_replaces_vertical_clamp() -> None:
    top_face = BakeTriangle(
        positions=((-0.4, -0.4, 0.4), (0.4, -0.4, 0.4), (0.0, 0.4, 0.4)),
        uvs=((0.1, 0.1), (0.9, 0.1), (0.5, 0.9)),
        normal=(0.0, 0.0, 1.0),
    )
    sources = {
        "FRONT": _solid_source((0.8, 0.1, 0.1, 1.0)),
        "RIGHT": _solid_source((0.8, 0.1, 0.1, 1.0)),
        "BACK": _solid_source((0.8, 0.1, 0.1, 1.0)),
    }
    without_top, _ = rasterize_atlas(
        (top_face,), sources, 32, 0, (0.0, 0.0, 0.0), 1.0, harmonize_colors=False
    )
    sources["TOP"] = _solid_source((0.1, 0.1, 0.8, 1.0))
    with_top, metrics = rasterize_atlas(
        (top_face,), sources, 32, 0, (0.0, 0.0, 0.0), 1.0, harmonize_colors=False
    )
    center = (12 * 32 + 16) * 4
    assert without_top[center] > without_top[center + 2]
    assert with_top[center + 2] > with_top[center]
    assert "TOP" in metrics["view_gains"]


def test_resolve_view_paths_accepts_three_or_six_views() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        files = {}
        for name in VIEW_NAMES:
            path = root / f"{name.lower()}.png"
            path.write_bytes(b"png")
            files[name] = path
        three = _resolve_view_paths([files["FRONT"], files["RIGHT"], files["BACK"]])
        assert tuple(three) == ("FRONT", "RIGHT", "BACK")
        six = _resolve_view_paths([files[name] for name in VIEW_NAMES])
        assert tuple(six) == VIEW_NAMES
        mapping = _resolve_view_paths({"front": files["FRONT"], "Right": files["RIGHT"], "back": files["BACK"], "top": files["TOP"]})
        assert tuple(mapping) == ("FRONT", "RIGHT", "BACK", "TOP")
        for bad in (
            [files["FRONT"], files["RIGHT"]],
            {"front": files["FRONT"], "right": files["RIGHT"]},
            {"front": files["FRONT"], "right": files["RIGHT"], "back": files["BACK"], "iso": files["TOP"]},
        ):
            try:
                _resolve_view_paths(bad)
            except ValueError:
                pass
            else:
                raise AssertionError("잘못된 뷰 경로 구성이 거부되어야 합니다.")


def test_barycentric_weights_reject_outside_and_degenerate_triangles() -> None:
    weights = barycentric_weights((0.25, 0.25), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
    assert weights is not None
    assert abs(sum(weights) - 1.0) < 1.0e-8
    assert barycentric_weights((1.0, 1.0), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0)) is None
    assert barycentric_weights((0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)) is None


def test_bilinear_sample_interpolates_four_pixels() -> None:
    pixels = (
        0.0, 0.0, 0.0, 1.0,
        1.0, 0.0, 0.0, 1.0,
        0.0, 1.0, 0.0, 1.0,
        1.0, 1.0, 0.0, 1.0,
    )
    color = bilinear_sample(pixels, 2, 2, 0.5, 0.5)
    assert color == (0.5, 0.5, 0.0, 1.0)


def test_foreground_bbox_supports_opaque_and_transparent_backgrounds() -> None:
    opaque = bytearray([255, 255, 255, 255] * 100)
    for y in range(3, 7):
        for x in range(2, 8):
            offset = (y * 10 + x) * 4
            opaque[offset : offset + 4] = bytes((80, 30, 10, 255))
    bbox = detect_foreground_bbox(opaque, 10, 10)
    assert bbox[0] <= 2 and bbox[1] <= 3 and bbox[2] >= 8 and bbox[3] >= 7

    transparent = bytearray([0, 0, 0, 0] * 100)
    for y in range(4, 6):
        for x in range(4, 6):
            offset = (y * 10 + x) * 4
            transparent[offset : offset + 4] = bytes((255, 0, 0, 255))
    bbox = detect_foreground_bbox(transparent, 10, 10)
    assert bbox[0] <= 4 and bbox[1] <= 4 and bbox[2] >= 6 and bbox[3] >= 6


def test_foreground_bbox_excludes_turnaround_labels_and_separators() -> None:
    width = height = 120
    pixels = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            checker = 220 if (x // 16 + y // 16) % 2 == 0 else 153
            # 생성 모델의 checker 경계에는 두 배경색 사이의 보간색이 생긴다.
            # 이 픽셀들이 격자망으로 연결돼 전체 이미지가 전경이 되면 안 된다.
            if x % 16 == 0 or y % 16 == 0:
                checker = 165 + ((x * 13 + y * 7) % 41)
            offset = (y * width + x) * 4
            pixels[offset : offset + 4] = bytes((checker, checker, checker, 255))

    def fill(left: int, bottom: int, right: int, top: int) -> None:
        for y in range(bottom, top):
            for x in range(left, right):
                offset = (y * width + x) * 4
                pixels[offset : offset + 4] = bytes((75, 35, 20, 255))

    fill(35, 30, 85, 90)  # 화면 중앙의 본체
    fill(88, 55, 94, 63)  # 본체와 가까운 작은 분리 부품
    for left in (42, 50, 58, 66, 74):
        fill(left, 103, left + 4, 111)  # 본체 위의 FRONT 라벨
    fill(20, 0, 24, height)  # 본체와 가까운 두꺼운 3면도 구분선
    fill(96, 0, 100, height)

    bbox = detect_foreground_bbox(pixels, width, height)
    assert bbox[0] <= 35 and bbox[1] <= 30
    assert bbox[2] >= 94 and bbox[3] >= 90
    assert bbox[0] > 24 and bbox[2] < 96
    assert bbox[3] < 103


def test_depth_buffer_keeps_camera_nearest_surface() -> None:
    back = BakeTriangle(
        positions=((-0.4, 0.2, -0.4), (0.4, 0.2, -0.4), (0.0, 0.2, 0.4)),
        uvs=((0.0, 0.0), (1.0, 0.0), (0.5, 1.0)),
        normal=(0.0, -1.0, 0.0),
    )
    front = BakeTriangle(
        positions=((-0.4, -0.3, -0.4), (0.4, -0.3, -0.4), (0.0, -0.3, 0.4)),
        uvs=((0.0, 0.0), (1.0, 0.0), (0.5, 1.0)),
        normal=(0.0, -1.0, 0.0),
    )
    depth = build_depth_buffer((back, front), "FRONT", (0.0, 0.0, 0.0), 1.0, 16)
    assert max(depth) > 0.29


def test_dilation_expands_nearest_color_only_to_padding_distance() -> None:
    rgba = bytearray(5 * 5 * 4)
    occupied = bytearray(5 * 5)
    center = 2 * 5 + 2
    rgba[center * 4 : center * 4 + 4] = bytes((10, 20, 30, 255))
    occupied[center] = 1
    assert dilate_rgba(rgba, occupied, 5, 5, 1) == 4
    assert occupied[center - 1] == 2
    assert occupied[0] == 0


def test_rasterizer_uses_front_color_and_creates_padding() -> None:
    triangle = BakeTriangle(
        positions=((-0.4, -0.4, -0.4), (0.4, -0.4, -0.4), (0.0, -0.4, 0.4)),
        uvs=((0.2, 0.2), (0.8, 0.2), (0.5, 0.8)),
        normal=(0.0, -1.0, 0.0),
    )
    sources = {
        "FRONT": _solid_source((0.8, 0.1, 0.05, 1.0)),
        "RIGHT": _solid_source((0.05, 0.8, 0.1, 1.0)),
        "BACK": _solid_source((0.05, 0.1, 0.8, 1.0)),
    }
    rgba, metrics = rasterize_atlas((triangle,), sources, 32, 2, (0.0, 0.0, 0.0), 1.0)
    assert metrics["filled_pixels"] > 100
    assert metrics["dilated_pixels"] > 0
    center = (16 * 32 + 16) * 4
    assert rgba[center] > rgba[center + 1] > rgba[center + 2]
    assert rgba[center + 3] == 255


def test_rasterizer_blocks_rear_surface_with_screen_depth() -> None:
    front = BakeTriangle(
        positions=((-0.4, -0.35, -0.4), (0.4, -0.35, -0.4), (0.0, -0.35, 0.4)),
        uvs=((0.05, 0.05), (0.45, 0.05), (0.25, 0.45)),
        normal=(0.0, -1.0, 0.0),
    )
    rear = BakeTriangle(
        positions=((-0.4, 0.25, -0.4), (0.4, 0.25, -0.4), (0.0, 0.25, 0.4)),
        uvs=((0.55, 0.05), (0.95, 0.05), (0.75, 0.45)),
        normal=(0.0, -1.0, 0.0),
    )
    sources = {
        "FRONT": _solid_source((0.8, 0.1, 0.05, 1.0)),
        "RIGHT": _solid_source((0.05, 0.8, 0.1, 1.0)),
        "BACK": _solid_source((0.05, 0.1, 0.8, 1.0)),
    }
    rgba, metrics = rasterize_atlas(
        (front, rear), sources, 32, 0, (0.0, 0.0, 0.0), 1.0
    )
    assert metrics["occluded_samples"] > 0
    # 가려진 뒷면은 전체 평균색이 아니라 같은 시점의 같은 좌표 색을 다시 쓴다.
    assert metrics["occluded_fallback_pixels"] > 0
    assert metrics["fallback_pixels"] == 0
    rear_pixel = (8 * 32 + 24) * 4
    assert rgba[rear_pixel] > rgba[rear_pixel + 1]
    assert rgba[rear_pixel] > rgba[rear_pixel + 2]
    assert rgba[rear_pixel + 3] == 255


def test_png_encoder_writes_square_srgb_rgba_png() -> None:
    png = encode_srgb_png(bytes((255, 0, 0, 255) * 4), 2, 2)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"sRGB" in png
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (2, 2)


def test_srgb_linear_roundtrip_preserves_midtones() -> None:
    linear = _srgb_to_linear(128 / 255.0)
    assert abs(linear - 0.2158605) < 1.0e-6
    assert _linear_to_srgb_byte(linear) == 128


def test_8192_resolution_is_rejected_with_4096_guidance() -> None:
    triangle = BakeTriangle(
        positions=((0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.0, 0.1)),
        uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        normal=(0.0, -1.0, 0.0),
    )
    sources = {name: _solid_source((0.5, 0.5, 0.5, 1.0)) for name in ("FRONT", "RIGHT", "BACK")}
    try:
        rasterize_atlas((triangle,), sources, 8192, 16, (0.0, 0.0, 0.0), 1.0)
    except ValueError as error:
        assert "4096" in str(error)
    else:
        raise AssertionError("8192px CPU Atlas 요청이 거부되어야 합니다.")


class _Loop:
    def __init__(self, vertex_index: int) -> None:
        self.vertex_index = vertex_index


class _Polygon:
    def __init__(self, loop_start: int, loop_total: int) -> None:
        self.loop_start = loop_start
        self.loop_total = loop_total


class _Mesh:
    def __init__(self, vertices: int, loops: list[int], polygons: list[tuple[int, int]]) -> None:
        self.vertices = [None] * vertices
        self.loops = [_Loop(index) for index in loops]
        self.polygons = [_Polygon(start, total) for start, total in polygons]


def _quad_mesh() -> _Mesh:
    return _Mesh(4, [0, 1, 2, 3], [(0, 4)])


def test_generated_topology_detects_modifier_output() -> None:
    original = _quad_mesh()
    assert _generated_topology(original, _quad_mesh()) is False
    subdivided = _Mesh(9, list(range(16)), [(0, 4), (4, 4), (8, 4), (12, 4)])
    assert _generated_topology(original, subdivided) is True


def _uv_triangle(uvs) -> BakeTriangle:
    return BakeTriangle(
        positions=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        uvs=uvs,
        normal=(0.0, -1.0, 0.0),
    )


def test_outside_atlas_flags_only_real_offsets() -> None:
    inside = _uv_triangle(((0.1, 0.1), (0.9, 0.1), (0.1, 0.9)))
    mirrored = _uv_triangle(((1.1, 0.1), (1.9, 0.1), (1.1, 0.9)))
    degenerate = _uv_triangle(((1.0, 0.0), (1.0, 1.0), (1.0, 0.5)))
    partial = _uv_triangle(((0.9, 0.1), (1.4, 0.1), (0.9, 0.9)))
    assert _outside_atlas(inside) is False
    assert _outside_atlas(mirrored) is True
    assert _outside_atlas(degenerate) is False
    assert _outside_atlas(partial) is False


def test_extend_foreground_fills_background_with_nearest_subject_color() -> None:
    width = height = 5
    pixels = []
    for y in range(height):
        for x in range(width):
            if x == 2 and y == 2:
                pixels.extend((0.9, 0.2, 0.1, 1.0))
            else:
                pixels.extend((1.0, 1.0, 1.0, 1.0))

    def is_foreground(color):
        return color[0] < 0.95

    filled, distance = extend_foreground(pixels, width, height, is_foreground)
    center = (2 * width + 2) * 4
    corner = (0 * width + 0) * 4
    assert distance[2 * width + 2] == 0.0
    assert abs(distance[0] - (8 ** 0.5)) < 1.0e-5
    # 배경이던 모서리도 유일한 전경 색을 그대로 받는다.
    assert filled[corner : corner + 4] == filled[center : center + 4]


def test_aligned_sampler_extends_near_edges_and_gives_up_far_from_subject() -> None:
    """생성 실루엣이 모델보다 좁을 때의 두 경우를 나눈다.

    피사체 경계 상자 안이라도 실제로 그려진 형상 밖일 수 있다. 가까우면 최근접
    전경 색으로 이어 붙이고, 멀면 다른 시점이 채우도록 표본을 포기해야 한다.
    """

    from uvmapping.texture_bake import _aligned_source_sample

    width = height = 64
    pixels = []
    for y in range(height):
        for x in range(width):
            drawn = x < 6 or x >= width - 6
            pixels.extend((0.8, 0.2, 0.1, 1.0) if drawn else (1.0, 1.0, 1.0, 1.0))
    source = build_raster_source(
        width=width,
        height=height,
        pixels=pixels,
        subject_bbox=(0, 0, width, height),
        background=(1.0, 1.0, 1.0, 1.0),
        background_threshold=0.2,
        fallback_color=(0.8, 0.2, 0.1, 1.0),
    )
    bbox = (0.0, 0.0, 1.0, 1.0)
    # 그려진 형상 안: 신뢰도 1.0.
    inside = _aligned_source_sample(source, (2.5 / width, 0.5, 0.0), bbox)
    assert inside is not None and inside[1] == 1.0
    # 그려진 형상 바로 바깥: 최근접 전경 색을 이어 붙이되 신뢰도는 낮아진다.
    near_edge = _aligned_source_sample(source, (6.5 / width, 0.5, 0.0), bbox)
    assert near_edge is not None
    color, confidence = near_edge
    assert color[0] > color[1]
    assert 0.05 <= confidence < 1.0
    farther = _aligned_source_sample(source, (8.5 / width, 0.5, 0.0), bbox)
    assert farther is not None and farther[1] < confidence
    # 형상에서 한참 떨어진 가운데: 다른 시점에 양보한다.
    assert _aligned_source_sample(source, (0.5, 0.5, 0.0), bbox) is None


def _rect_mask(width: int, height: int, rects) -> bytearray:
    mask = bytearray(width * height)
    for left, bottom, right, top in rects:
        for y in range(bottom, top):
            for x in range(left, right):
                mask[y * width + x] = 1
    return mask


def _disc_mask(width: int, height: int, cx: float, cy: float, radius: float) -> bytearray:
    mask = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= radius * radius:
                mask[y * width + x] = 1
    return mask


def test_align_silhouette_recovers_scale_and_offset_despite_loose_bbox() -> None:
    """경계 상자가 라벨 등으로 느슨해도 실루엣 IoU로 스케일·위치를 복원해야 한다."""

    mask_resolution = 128
    model_mask = _disc_mask(mask_resolution, mask_resolution, 64.0, 64.0, 30.0)
    model_bbox = (34 / 128, 34 / 128, 94 / 128, 94 / 128)
    width = height = 200
    # 1.1배 확대하고 (+7, -5) 이동한 생성 실루엣.
    source_mask = _disc_mask(width, height, 107.0, 95.0, 33.0)
    loose_bbox = (64, 56, 150, 136)
    alignment = align_silhouette(
        model_mask, mask_resolution, model_bbox, source_mask, width, height, loose_bbox, warp=False
    )
    assert alignment.iou >= 0.95, alignment.iou
    expected_scale = 66.0
    assert abs(alignment.scale_x - expected_scale) <= expected_scale * 0.03, alignment.scale_x
    assert abs(alignment.scale_y - expected_scale) <= expected_scale * 0.03, alignment.scale_y
    assert abs(alignment.offset_x - 74.0) <= expected_scale * 0.03, alignment.offset_x
    assert abs(alignment.offset_y - 62.0) <= expected_scale * 0.03, alignment.offset_y


def test_row_warp_stretches_only_wider_rows_to_source_edge() -> None:
    """AI가 일부 행만 넓게 그리면 그 행에서만 모델 우측 끝이 소스 우측 끝으로 가야 한다."""

    mask_resolution = 128
    model_mask = _rect_mask(mask_resolution, mask_resolution, [(44, 24, 84, 104)])
    model_bbox = (44 / 128, 24 / 128, 84 / 128, 104 / 128)
    width = height = 200
    # 기본 폭 44px, 가운데 20행만 양쪽으로 2px씩 넓힌 48px(약 9% 넓음, 중앙 유지).
    source_mask = _rect_mask(width, height, [(78, 56, 122, 144), (76, 90, 124, 110)])
    alignment = align_silhouette(
        model_mask, mask_resolution, model_bbox, source_mask, width, height, (76, 56, 124, 144), warp=True
    )
    assert alignment.row_warp is not None
    assert alignment.column_warp is None
    bulge_x, _y = alignment.map_to_source(1.0, (100.0 - alignment.offset_y) / alignment.scale_y)
    plain_x, _y = alignment.map_to_source(1.0, (70.0 - alignment.offset_y) / alignment.scale_y)
    assert abs(bulge_x - 124.0) <= 1.5, bulge_x
    assert abs(plain_x - 122.0) <= 1.5, plain_x
    left_x, _y = alignment.map_to_source(0.0, (100.0 - alignment.offset_y) / alignment.scale_y)
    assert abs(left_x - 76.0) <= 1.5, left_x


def _figure_mask(width: int, height: int, crotch: int) -> bytearray:
    """몸통·양팔·양다리 사각형으로 만든 A-포즈 실루엣. ``crotch``는 다리 사이 틈의 윗변."""

    torso = (60, crotch, 100, 150)
    head = (68, 150, 92, 180)
    left_arm = (30, 90, 60, 150)
    right_arm = (100, 90, 130, 150)
    left_leg = (60, 20, 78, crotch)
    right_leg = (82, 20, 100, crotch)
    return _rect_mask(width, height, [torso, head, left_arm, right_arm, left_leg, right_leg])


def test_warp_keeps_torso_centre_straight_on_articulated_figure() -> None:
    """팔·다리가 있는 형상에서 가랑이 높이만 달라도 몸통 중앙선이 흔들리면 안 된다."""

    mask_resolution = 200
    model_mask = _figure_mask(mask_resolution, mask_resolution, 70)
    model_bbox = (30 / 200, 20 / 200, 130 / 200, 180 / 200)
    width = height = 200
    source_mask = _figure_mask(width, height, 80)
    default_alignment = align_silhouette(
        model_mask, mask_resolution, model_bbox, source_mask, width, height, (30, 20, 130, 180)
    )
    # 기본은 워프 없이 전역 변환만이다.
    assert default_alignment.row_warp is None and default_alignment.column_warp is None
    alignment = align_silhouette(
        model_mask, mask_resolution, model_bbox, source_mask, width, height, (30, 20, 130, 180), warp=True
    )
    assert alignment.column_warp is None
    centres = [alignment.map_to_source(0.5, v / 100.0)[0] for v in range(0, 101)]
    neighbour_jumps = [abs(centres[i + 1] - centres[i]) for i in range(len(centres) - 1)]
    assert max(neighbour_jumps) <= 1.0, max(neighbour_jumps)
    global_x = alignment.map_global(0.5, 0.5)[0]
    assert max(abs(x - global_x) for x in centres) <= 2.0
    # 열 워프가 없으므로 같은 v에서 y는 u와 무관하다.
    ys = {round(alignment.map_to_source(u / 10.0, 0.5)[1], 6) for u in range(0, 11)}
    assert len(ys) == 1, ys


def test_identity_rows_are_not_mixed_into_warp_smoothing() -> None:
    """항등 줄 옆의 워프 줄이 항등과 평균되어 어정쩡한 이동을 얻으면 안 된다."""

    mask_resolution = 128
    model_mask = _rect_mask(mask_resolution, mask_resolution, [(44, 24, 84, 104)])
    model_bbox = (44 / 128, 24 / 128, 84 / 128, 104 / 128)
    width = height = 200
    # 위쪽 절반은 폭 44px 그대로, 아래쪽 절반은 양쪽으로 2px씩(약 9%) 넓힘.
    source_mask = _rect_mask(width, height, [(78, 100, 122, 144), (76, 56, 124, 100)])
    alignment = align_silhouette(
        model_mask, mask_resolution, model_bbox, source_mask, width, height, (76, 56, 124, 144), warp=True
    )
    assert alignment.row_warp is not None
    # 경계 바로 아래(워프 줄)와 바로 위(항등 줄)의 우측 끝은 각자 소스 폭을 따라야 한다.
    below_x, _y = alignment.map_to_source(1.0, (96.0 - alignment.offset_y) / alignment.scale_y)
    above_x, _y = alignment.map_to_source(1.0, (104.0 - alignment.offset_y) / alignment.scale_y)
    assert above_x < below_x, (above_x, below_x)


def test_prepare_sources_drops_shadow_outside_model_silhouette() -> None:
    """정합된 모델 실루엣 밖의 그림자는 색과 무관하게 배경이 되고, 여유 안 픽셀은 남아야 한다."""

    width = height = 64
    red = (1.0, 0.2, 0.2, 1.0)
    shadow = (0.3, 0.3, 0.3, 1.0)
    pixels = []
    for y in range(height):
        for x in range(width):
            if 20 <= x < 45 and 20 <= y < 44:
                pixels.extend(red)  # 피사체(모델보다 오른쪽 한 열 넓음)
            elif 20 <= x < 44 and 8 <= y < 14:
                pixels.extend(shadow)  # 피사체 아래 그림자
            else:
                pixels.extend((1.0, 1.0, 1.0, 1.0))
    source = build_raster_source(
        width=width,
        height=height,
        pixels=pixels,
        subject_bbox=(20, 20, 45, 44),
        background=(1.0, 1.0, 1.0, 1.0),
        background_threshold=0.2,
        fallback_color=(0.5, 0.5, 0.5, 1.0),
    )
    # 정제 전에는 그림자도 전경이다.
    assert source.foreground_distance[10 * width + 32] == 0.0
    depth = [float("-inf")] * (64 * 64)
    for y in range(16, 40):
        for x in range(16, 40):
            depth[y * 64 + x] = 0.0
    bbox = (16 / 64, 16 / 64, 40 / 64, 40 / 64)
    report: dict = {}
    prepared = prepare_sources({"FRONT": source}, {"FRONT": depth}, 64, {"FRONT": bbox}, report=report)
    refined = prepared["FRONT"]
    assert refined.alignment is not None and refined.alignment.iou >= 0.9, refined.alignment
    assert report["FRONT"]["accepted"] is True
    assert refined.foreground_distance[10 * width + 32] > 0.0
    assert refined.foreground_distance[30 * width + 32] == 0.0
    assert refined.foreground_distance[30 * width + 44] == 0.0
    assert refined.fallback_color[0] > 0.95 and refined.fallback_color[1] < 0.25
    # 정제된 마스크는 다음 단계가 재사용할 수 있게 보관된다.
    assert refined.foreground is not None and refined.foreground[10 * width + 32] == 0


def _mask_image(width: int, height: int, mask, color=(1.0, 0.2, 0.2, 1.0)) -> RasterSource:
    """마스크가 1인 곳만 색을 칠한 흰 배경 소스."""

    pixels = []
    for index in range(width * height):
        pixels.extend(color if mask[index] else (1.0, 1.0, 1.0, 1.0))
    xs = [index % width for index in range(width * height) if mask[index]]
    ys = [index // width for index in range(width * height) if mask[index]]
    return build_raster_source(
        width=width,
        height=height,
        pixels=pixels,
        subject_bbox=(min(xs), min(ys), max(xs) + 1, max(ys) + 1),
        background=(1.0, 1.0, 1.0, 1.0),
        background_threshold=0.2,
        fallback_color=(0.5, 0.5, 0.5, 1.0),
    )


def test_alignment_survives_aspect_mismatch_or_falls_back() -> None:
    """팔 벌린 모델(44×70)을 AI가 팔 내린 몸통(20×70)으로 그리면 세로를 희생해 맞추면 안 된다.

    정합을 받아들이면 세로 스케일과 머리 꼭대기 위치가 맞아야 하고, 아니면 거부해
    경계 상자 경로로 되돌아가야 한다. 어느 쪽이든 머리·발은 전경으로 남아야 한다.
    """

    mask_resolution = 128
    # 몸통 20×70 + 팔 띠 44×10.
    model_mask = _rect_mask(mask_resolution, mask_resolution, [(54, 29, 74, 99), (42, 70, 86, 80)])
    model_bbox = (42 / 128, 29 / 128, 86 / 128, 99 / 128)
    width = height = 200
    source_mask = _rect_mask(width, height, [(90, 65, 110, 135)])
    source = _mask_image(width, height, source_mask)
    depth = [float("-inf")] * (mask_resolution * mask_resolution)
    for index in range(mask_resolution * mask_resolution):
        if model_mask[index]:
            depth[index] = 0.0
    report: dict = {}
    prepared = prepare_sources(
        {"FRONT": source}, {"FRONT": depth}, mask_resolution, {"FRONT": model_bbox}, report=report
    )["FRONT"]
    summary = report["FRONT"]
    if prepared.alignment is None:
        assert summary["accepted"] is False and summary["iou"] < ALIGNMENT_MIN_IOU, summary
        # 거부되면 coarse 마스크가 그대로여야 한다.
        assert prepared.foreground_distance is source.foreground_distance
    else:
        assert summary["accepted"] is True
        assert abs(prepared.alignment.scale_y - 70.0) <= 70.0 * 0.05, prepared.alignment
        _x, top_y = prepared.alignment.map_to_source(0.5, 1.0)
        assert abs(top_y - 135.0) <= 3.0, top_y
    # 머리 꼭대기와 발끝은 어느 경로에서도 전경이다.
    assert prepared.foreground_distance[66 * width + 100] == 0.0
    assert prepared.foreground_distance[133 * width + 100] == 0.0


def test_low_iou_alignment_is_rejected_and_keeps_coarse_foreground() -> None:
    """실루엣이 전혀 다르면(IoU 낮음) 정합도 팽창 마스크도 쓰지 않아야 진짜 전경이 잘리지 않는다."""

    mask_resolution = 64
    model_mask = _rect_mask(mask_resolution, mask_resolution, [(8, 8, 56, 56)])
    model_bbox = (8 / 64, 8 / 64, 56 / 64, 56 / 64)
    width = height = 120
    # 모델은 큰 정사각형인데 AI는 가늘고 긴 막대 두 개를 멀찍이 그렸다.
    source_mask = _rect_mask(width, height, [(10, 10, 16, 110), (104, 10, 110, 110)])
    source = _mask_image(width, height, source_mask)
    depth = [0.0 if model_mask[index] else float("-inf") for index in range(mask_resolution * mask_resolution)]
    report: dict = {}
    prepared = prepare_sources(
        {"FRONT": source}, {"FRONT": depth}, mask_resolution, {"FRONT": model_bbox}, report=report
    )["FRONT"]
    assert report["FRONT"]["accepted"] is False
    assert report["FRONT"]["iou"] < ALIGNMENT_MIN_IOU
    assert prepared.alignment is None
    assert prepared.foreground_distance is source.foreground_distance
    assert prepared.foreground_distance[60 * width + 12] == 0.0
    assert prepared.foreground_distance[60 * width + 107] == 0.0


def test_detached_shadow_below_subject_is_not_absorbed_by_warp() -> None:
    """발밑에 떨어진 그림자 띠가 있어도 열 워프가 모델을 그쪽으로 늘리면 안 되고, 그림자는 배경이 되어야 한다."""

    mask_resolution = 128
    model_mask = _disc_mask(mask_resolution, mask_resolution, 64.0, 64.0, 40.0)
    model_bbox = (24 / 128, 24 / 128, 104 / 128, 104 / 128)
    width = height = 160
    source_mask = _disc_mask(width, height, 82.0, 84.0, 42.0)
    for y in range(20, 30):  # 원판 아래로 12px 떨어진 그림자 띠
        for x in range(50, 114):
            source_mask[y * width + x] = 1
    source = _mask_image(width, height, source_mask)
    depth = [0.0 if model_mask[index] else float("-inf") for index in range(mask_resolution * mask_resolution)]
    report: dict = {}
    prepared = prepare_sources(
        {"FRONT": source}, {"FRONT": depth}, mask_resolution, {"FRONT": model_bbox}, report=report
    )["FRONT"]
    assert report["FRONT"]["accepted"] is True, report
    alignment = prepared.alignment
    assert alignment is not None
    # 원판 바닥(v=0)은 그림자가 아니라 원판 아래 가장자리(y≈42)로 가야 한다.
    _x, bottom_y = alignment.map_to_source(0.5, 0.0)
    assert abs(bottom_y - 42.0) <= 3.0, bottom_y
    assert prepared.foreground_distance[25 * width + 82] > 0.0
    assert prepared.foreground_distance[84 * width + 82] == 0.0


def test_dilate_mask_expands_square_radius_without_row_leak() -> None:
    width, height = 9, 7
    mask = bytearray(width * height)
    mask[3 * width + 0] = 1  # 왼쪽 가장자리 픽셀: 오른쪽 가장자리로 새면 안 된다.
    mask[1 * width + 6] = 1
    dilated = dilate_mask(mask, width, height, 1)
    assert dilated[2 * width + 0] == 1 and dilated[4 * width + 1] == 1
    assert dilated[3 * width + 8] == 0 and dilated[2 * width + 8] == 0 and dilated[4 * width + 8] == 0
    assert dilated[0 * width + 5] == 1 and dilated[2 * width + 7] == 1 and dilated[0 * width + 8] == 0
    assert sum(dilated) == 6 + 9
    assert dilate_mask(mask, width, height, 0) == mask
    assert sum(dilate_mask(bytearray(width * height), width, height, 2)) == 0


def _closed_cube(size: float = 0.4) -> tuple[BakeTriangle, ...]:
    """면마다 UV 조각을 따로 두고 법선은 면 법선인 정육면체."""

    s = size
    faces = (
        ((0.0, -1.0, 0.0), ((-s, -s, -s), (s, -s, -s), (s, -s, s), (-s, -s, s))),
        ((1.0, 0.0, 0.0), ((s, -s, -s), (s, s, -s), (s, s, s), (s, -s, s))),
        ((0.0, 1.0, 0.0), ((s, s, -s), (-s, s, -s), (-s, s, s), (s, s, s))),
        ((-1.0, 0.0, 0.0), ((-s, s, -s), (-s, -s, -s), (-s, -s, s), (-s, s, s))),
        ((0.0, 0.0, 1.0), ((-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s))),
        ((0.0, 0.0, -1.0), ((-s, s, -s), (s, s, -s), (s, -s, -s), (-s, -s, -s))),
    )
    triangles = []
    for index, (normal, corners) in enumerate(faces):
        column, row = index % 3, index // 3
        u0, v0 = column / 3 + 0.02, row / 2 + 0.02
        u1, v1 = (column + 1) / 3 - 0.02, (row + 1) / 2 - 0.02
        uvs = ((u0, v0), (u1, v0), (u1, v1), (u0, v1))
        for a, b, c in ((0, 1, 2), (0, 2, 3)):
            triangles.append(
                BakeTriangle(
                    positions=(corners[a], corners[b], corners[c]),
                    uvs=(uvs[a], uvs[b], uvs[c]),
                    normal=normal,
                )
            )
    return tuple(triangles)


def test_cube_top_face_without_top_source_is_recovered_through_occlusion_fallback() -> None:
    """윗면은 모든 측면 뷰에서 가려지지만, 재표본 경로가 살려 평균색 폴백으로 떨어지지 않아야 한다."""

    cube = _closed_cube()
    side = (0.1, 0.7, 0.2, 1.0)
    sources = {
        "FRONT": _solid_source(side),
        "RIGHT": _solid_source(side),
        "BACK": _solid_source(side),
    }
    resolution = 48
    rgba, metrics = rasterize_atlas(cube, sources, resolution, 0, (0.0, 0.0, 0.0), 1.0, harmonize_colors=False)
    assert metrics["fallback_pixels"] == 0, metrics
    assert metrics["occluded_fallback_pixels"] > 0, metrics
    # 윗면(index 4 → column 1, row 1) 중앙 픽셀은 측면 색이어야 한다.
    x = int((1 / 3 + 1 / 6) * resolution)
    y = int((0.5 + 0.25) * resolution)
    offset = (y * resolution + x) * 4
    assert rgba[offset + 1] > rgba[offset] and rgba[offset + 1] > rgba[offset + 2], rgba[offset : offset + 3]


def test_rasterizer_reports_view_alignment() -> None:
    triangle = BakeTriangle(
        positions=((-0.4, -0.4, -0.4), (0.4, -0.4, -0.4), (0.0, -0.4, 0.4)),
        uvs=((0.2, 0.2), (0.8, 0.2), (0.5, 0.8)),
        normal=(0.0, -1.0, 0.0),
    )
    sources = {
        "FRONT": _solid_source((0.8, 0.1, 0.05, 1.0)),
        "RIGHT": _solid_source((0.05, 0.8, 0.1, 1.0)),
        "BACK": _solid_source((0.05, 0.1, 0.8, 1.0)),
    }
    _rgba, metrics = rasterize_atlas((triangle,), sources, 32, 2, (0.0, 0.0, 0.0), 1.0)
    # 소스마다 정합 요약이 있어야 한다(거부된 소스도 accepted=False로 남는다).
    assert set(metrics["view_alignment"]) == set(sources)
    for summary in metrics["view_alignment"].values():
        assert set(summary) == {
            "scale", "offset", "iou", "warped", "accepted",
            "coverage", "segment_mismatch_ratio", "bright_hole_ratio", "passed",
        }, set(summary)
        assert 0.0 <= summary["iou"] <= 1.0
        assert summary["accepted"] == (summary["iou"] >= ALIGNMENT_MIN_IOU)
    assert metrics["view_alignment"]["FRONT"]["accepted"] is True
    # 모서리로만 보이는 RIGHT 뷰는 실루엣이 없어 정합이 거부되어야 한다.
    assert metrics["view_alignment"]["RIGHT"]["accepted"] is False


def test_partial_bake_paints_unpainted_pixels_with_marker_color() -> None:
    # 정면 삼각형과 뒷면 삼각형. 소스는 FRONT 한 장뿐이다.
    front = BakeTriangle(
        positions=((-0.4, -0.4, -0.4), (0.4, -0.4, -0.4), (0.0, -0.4, 0.4)),
        uvs=((0.05, 0.05), (0.45, 0.05), (0.25, 0.45)),
        normal=(0.0, -1.0, 0.0),
    )
    back = BakeTriangle(
        positions=((0.4, 0.4, -0.4), (-0.4, 0.4, -0.4), (0.0, 0.4, 0.4)),
        uvs=((0.55, 0.55), (0.95, 0.55), (0.75, 0.95)),
        normal=(0.0, 1.0, 0.0),
    )
    sources = {"FRONT": _solid_source((0.8, 0.1, 0.05, 1.0))}
    try:
        rasterize_atlas((front, back), sources, 32, 0, (0.0, 0.0, 0.0), 1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("필수 소스가 없으면 기본 베이크는 거부해야 합니다.")
    rgba, metrics = rasterize_atlas(
        (front, back),
        sources,
        32,
        0,
        (0.0, 0.0, 0.0),
        1.0,
        unpainted_color=(0.25, 0.25, 0.25),
        require_all_sources=False,
    )
    assert metrics["unpainted_pixels"] > 0
    assert metrics["unpainted_pixels"] == metrics["fallback_pixels"]
    front_center = (int(0.18 * 32) * 32 + int(0.25 * 32)) * 4
    back_center = (int(0.68 * 32) * 32 + int(0.75 * 32)) * 4
    assert rgba[front_center] > rgba[front_center + 1] > rgba[front_center + 2]
    # 뒷면은 소스가 없으므로 지정한 회색(선형 0.25 → sRGB 137) 하나로 채워진다.
    assert rgba[back_center] == rgba[back_center + 1] == rgba[back_center + 2]
    assert 130 <= rgba[back_center] <= 145
    assert set(_resolve_view_paths.__code__.co_varnames) >= {"require_all"}


def test_disabling_view_substitution_leaves_side_and_top_faces_unpainted() -> None:
    """순차 모드 가이드는 FRONT만 있을 때 윗면·좌측면이 회색 미채색으로 남아야 한다."""

    cube = _closed_cube()
    sources = {"FRONT": _solid_source((0.8, 0.1, 0.05, 1.0))}
    resolution = 48
    marker = (0.25, 0.25, 0.25)
    common = dict(unpainted_color=marker, require_all_sources=False, harmonize_colors=False)
    _rgba, substituted = rasterize_atlas(cube, sources, resolution, 0, (0.0, 0.0, 0.0), 1.0, **common)
    rgba, strict = rasterize_atlas(
        cube, sources, resolution, 0, (0.0, 0.0, 0.0), 1.0, allow_view_substitution=False, **common
    )
    # 대체를 허용하면 윗면이 FRONT 색으로 늘려 채워지지만, 막으면 미채색으로 남는다.
    assert strict["unpainted_pixels"] > substituted["unpainted_pixels"] > 0, (strict, substituted)

    def pixel(column: int, row: int) -> tuple[int, int, int]:
        x = int((column / 3 + 1 / 6) * resolution)
        y = int((row / 2 + 0.25) * resolution)
        offset = (y * resolution + x) * 4
        return rgba[offset], rgba[offset + 1], rgba[offset + 2]

    front = pixel(0, 0)
    assert front[0] > front[1] > front[2], front
    for column, row in ((0, 1), (1, 1), (2, 1)):  # LEFT, TOP, BOTTOM 면
        color = pixel(column, row)
        assert color[0] == color[1] == color[2] and 130 <= color[0] <= 145, (column, row, color)
    try:
        rasterize_atlas(cube, sources, resolution, 0, (0.0, 0.0, 0.0), 1.0, require_all_sources=False, allow_view_substitution=False)
    except ValueError:
        pass
    else:
        raise AssertionError("미채색 색 없이 뷰 대체를 끄면 거부해야 합니다.")


def test_bright_achromatic_gap_on_light_background_is_background() -> None:
    """배경(0.93)보다 밝은 순백 틈은 색 거리로는 전경이지만 배경으로 판정되어야 한다."""

    width = height = 40
    background = (0.93, 0.93, 0.93, 1.0)
    pixels = []
    for y in range(height):
        for x in range(width):
            if 8 <= x < 32 and 6 <= y < 34:
                if 18 <= x < 22:
                    pixels.extend((1.0, 1.0, 1.0, 1.0))  # 팔·몸통 사이 순백 틈
                else:
                    pixels.extend((0.2, 0.25, 0.15, 1.0))
            else:
                pixels.extend(background)
    # 거리 0.07은 임계(0.0693·0.55)를 넘어 색 거리만으로는 전경이 된다.
    mask = coarse_foreground_mask(pixels, width, height, background, 0.0693)
    assert mask[20 * width + 12] == 1
    assert mask[20 * width + 20] == 0, "순백 틈이 전경으로 남았습니다."
    # 밝은 흰 옷(1.0)이라도 배경이 어두우면 이 규칙은 꺼진다.
    dark = (0.1, 0.1, 0.1, 1.0)
    dark_pixels = [value for index in range(width * height) for value in (
        (1.0, 1.0, 1.0, 1.0) if 8 <= index % width < 32 and 6 <= index // width < 34 else dark
    )]
    assert coarse_foreground_mask(dark_pixels, width, height, dark, 0.2)[20 * width + 20] == 1


def test_background_flood_follows_gradient_but_stops_at_subject() -> None:
    width = height = 48
    background = (0.90, 0.90, 0.90, 1.0)
    pixels = []
    for y in range(height):
        for x in range(width):
            if 16 <= x < 32 and 16 <= y < 32:
                pixels.extend((0.2, 0.3, 0.6, 1.0))
            else:
                # 왼쪽 0.90에서 오른쪽 1.0까지 완만한 그라데이션
                shade = 0.90 + 0.10 * x / (width - 1)
                pixels.extend((shade, shade, shade, 1.0))
    flood = background_flood_mask(pixels, width, height, background)
    assert flood[8 * width + 46] == 1, "그라데이션 끝이 배경으로 이어져야 합니다."
    assert flood[24 * width + 24] == 0
    # 플러드필이 합쳐진 coarse 마스크에서도 오른쪽 그라데이션은 배경이다.
    mask = coarse_foreground_mask(pixels, width, height, background, 0.06)
    assert mask[8 * width + 46] == 0 and mask[24 * width + 24] == 1


def test_view_gains_ignore_scattered_ratios_and_keep_small_differences() -> None:
    """정합이 어긋나 표본 비율이 흩어지면 보정하지 않고, 2% 이내 차이는 1.0 근처를 유지한다."""

    triangles = _slanted_triangles(12)
    sources = {
        "FRONT": _solid_source((0.40, 0.40, 0.40, 1.0)),
        "RIGHT": _solid_source((0.392, 0.392, 0.392, 1.0)),
        "BACK": _solid_source((0.1, 0.1, 0.8, 1.0)),
    }
    gains = estimate_view_gains(triangles, sources, *_gain_inputs(triangles, sources))
    assert all(abs(value - 1.0) <= 0.03 for value in gains["RIGHT"]), gains["RIGHT"]

    # RIGHT 소스를 좌우 절반으로 갈라 표본마다 비율이 0.5 또는 2.0으로 흩어지게 한다
    # (정합이 어긋나 서로 다른 부위를 비교하는 상황의 축소판).
    checker = []
    for index in range(16):
        checker.extend((0.2, 0.2, 0.2, 1.0) if index % 4 < 2 else (0.8, 0.8, 0.8, 1.0))
    sources["RIGHT"] = build_raster_source(
        width=4, height=4, pixels=checker, subject_bbox=(0, 0, 4, 4),
        background=(1.0, 1.0, 1.0, 1.0), background_threshold=0.05, fallback_color=(0.5, 0.5, 0.5, 1.0),
    )
    scattered, counts = _estimate_view_gains(triangles, sources, *_gain_inputs(triangles, sources))
    assert counts[("FRONT", "RIGHT")] >= 32
    assert scattered["RIGHT"] == (1.0, 1.0, 1.0), scattered["RIGHT"]


def test_silhouette_match_detects_arms_drawn_against_body() -> None:
    """팔 벌린 모델(행당 3구간) 위에 팔을 붙여 그린 그림(1구간)은 구간 불일치로 잡혀야 한다."""

    width = height = 96
    # 모델: 몸통 + 양쪽으로 떨어진 팔 두 개.
    model = _rect_mask(width, height, [(40, 10, 56, 90), (14, 40, 30, 80), (66, 40, 82, 80)])
    # 그림: 팔이 몸통에 붙어 하나의 넓은 덩어리 + 몸통과 팔 사이 순백 틈은 배경(0).
    drawn = _rect_mask(width, height, [(24, 10, 72, 90)])
    mismatch = evaluate_silhouette_match(model, width, height, drawn, iou=0.87, margin=3)
    assert mismatch["segment_mismatch_ratio"] > SILHOUETTE_SEGMENT_MISMATCH_LIMIT, mismatch
    assert not silhouette_match_passed(mismatch)
    # 같은 실루엣을 그대로 그렸으면 통과한다.
    same = evaluate_silhouette_match(model, width, height, model, iou=0.95, margin=3)
    assert same["segment_mismatch_ratio"] < 0.1 and same["bright_hole_ratio"] == 0.0, same
    assert same["coverage"] == 1.0
    assert silhouette_match_passed(same)
    # 실루엣은 같지만 몸통 한가운데가 배경으로 뚫린 그림은 구멍 비율로 잡힌다.
    holed = bytearray(model)
    for y in range(30, 70):
        for x in range(44, 52):
            holed[y * width + x] = 0
    hole = evaluate_silhouette_match(model, width, height, holed, iou=0.95, margin=3)
    assert hole["bright_hole_ratio"] > SILHOUETTE_HOLE_LIMIT, hole
    assert not silhouette_match_passed(hole)


def test_compare_rendered_view_scores_matching_and_mismatching_colors() -> None:
    width = height = 32
    mask = _rect_mask(width, height, [(8, 8, 24, 24)])
    source = _mask_image(width, height, mask, color=(0.6, 0.3, 0.1, 1.0))
    # 렌더는 같은 프레임(0-1)에 같은 색을 찍은 이미지.
    same = []
    other = []
    for index in range(width * height):
        same.extend((0.6, 0.3, 0.1, 1.0) if mask[index] else (1.0, 1.0, 1.0, 1.0))
        other.extend((0.1, 0.6, 0.6, 1.0) if mask[index] else (1.0, 1.0, 1.0, 1.0))
    bbox = (8 / 32, 8 / 32, 24 / 32, 24 / 32)
    good = compare_rendered_view(same, width, height, source, mask, 32, bbox)
    bad = compare_rendered_view(other, width, height, source, mask, 32, bbox)
    assert good["mean_color_error"] < 1.0e-6 and good["coverage"] == 1.0, good
    assert good["score"] > 0.9 and bad["score"] < good["score"], (good, bad)
    assert bad["mean_color_error"] > 0.3, bad


def test_verification_sheet_places_first_row_on_top() -> None:
    red = ([0.9, 0.1, 0.1, 1.0] * 4, 2, 2)
    blue = ([0.1, 0.1, 0.9, 1.0] * 4, 2, 2)
    rgba, width, height = compose_verification_sheet([[red, None], [blue, blue]], 4)
    assert (width, height) == (8, 8)
    top_left = ((height - 1) * width + 0) * 4
    bottom_left = (0 * width + 0) * 4
    top_right = ((height - 1) * width + 6) * 4
    assert rgba[top_left] > rgba[top_left + 2], "첫 행이 위쪽이어야 합니다."
    assert rgba[bottom_left + 2] > rgba[bottom_left]
    assert rgba[top_right : top_right + 3] == b"\xff\xff\xff"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"texture bake pure tests: {len(tests)} passed")
