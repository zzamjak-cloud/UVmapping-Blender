"""Blender 없이 실행하는 CPU Diffuse/Albedo Atlas 회귀 테스트."""

from __future__ import annotations

from pathlib import Path
import struct
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_bake import (
    BakeTriangle,
    RasterSource,
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
    pixels = color * 16
    return RasterSource(
        width=4,
        height=4,
        pixels=pixels,
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
    _rgba, metrics = rasterize_atlas(
        (front, rear), sources, 32, 0, (0.0, 0.0, 0.0), 1.0
    )
    assert metrics["occluded_samples"] > 0
    assert metrics["fallback_pixels"] > 0


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


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"texture bake pure tests: {len(tests)} passed")
