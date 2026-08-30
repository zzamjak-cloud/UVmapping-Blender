"""AI 3면도를 공유 UV Atlas의 Diffuse/Albedo 텍스처로 투영한다.

Blender 데이터 접근은 :func:`bake_diffuse`에만 모으고, 좌표 투영과 래스터화는
Blender 없이도 회귀 테스트할 수 있는 순수 파이썬 함수로 유지한다.
"""

from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass
import binascii
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Iterable, Mapping, Sequence
import uuid
import zlib

try:  # Blender 밖의 순수 테스트에서도 이 모듈을 가져올 수 있어야 한다.
    import bpy  # type: ignore
    from mathutils import Vector  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - Blender 테스트에서 별도 검증
    bpy = None
    Vector = None


VIEW_NAMES = ("FRONT", "RIGHT", "BACK", "LEFT")
SOURCE_VIEW_NAMES = ("FRONT", "RIGHT", "BACK")
MAX_ATLAS_RESOLUTION = 4096
_IMAGE_MARKER = "uvmapping_ai_albedo"
_MATERIAL_MARKER = "uvmapping_ai_albedo"

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
BBox = tuple[int, int, int, int]


@dataclass(frozen=True)
class BakeTriangle:
    """월드 좌표 삼각형과 각 코너의 UV 좌표."""

    positions: tuple[Vec3, Vec3, Vec3]
    uvs: tuple[Vec2, Vec2, Vec2]
    normal: Vec3


@dataclass(frozen=True)
class RasterSource:
    """Blender에서 선형 색 공간으로 읽은 생성 이미지."""

    width: int
    height: int
    pixels: Sequence[float]
    subject_bbox: BBox
    background: tuple[float, float, float, float]
    background_threshold: float
    fallback_color: tuple[float, float, float, float]


@dataclass(frozen=True)
class ProjectionFrame:
    """모든 뷰가 공유하는 직교 카메라 프레이밍."""

    center: Vec3
    scale: float
    projected_bboxes: Mapping[str, tuple[float, float, float, float]]


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return min(maximum, max(minimum, value))


def _as_unit(value: float | int) -> float:
    numeric = float(value)
    return _clamp(numeric / 255.0 if numeric > 1.0 else numeric)


def project_point(point: Vec3, view: str, center: Vec3, scale: float) -> tuple[float, float, float]:
    """월드 점을 정사각 직교 화면에 투영한다.

    depth는 카메라 방향 축 값이 클수록 가까운 값이다. FRONT 카메라는 -Y,
    RIGHT는 +X, BACK은 +Y, 가상 LEFT는 -X에서 모델을 바라본다.
    """

    if scale <= 0.0:
        raise ValueError("투영 scale은 0보다 커야 합니다.")
    x = point[0] - center[0]
    y = point[1] - center[1]
    z = point[2] - center[2]
    name = view.upper()
    if name == "FRONT":
        screen_x, depth = x, -y
    elif name == "RIGHT":
        screen_x, depth = y, x
    elif name == "BACK":
        screen_x, depth = -x, y
    elif name == "LEFT":
        screen_x, depth = -y, -x
    else:
        raise ValueError(f"지원하지 않는 투영 뷰입니다: {view}")
    return 0.5 + screen_x / scale, 0.5 + z / scale, depth


def barycentric_weights(point: Vec2, a: Vec2, b: Vec2, c: Vec2) -> tuple[float, float, float] | None:
    """2D 삼각형 내부 점의 barycentric 가중치를 반환한다."""

    denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(denominator) < 1.0e-12:
        return None
    first = ((b[1] - c[1]) * (point[0] - c[0]) + (c[0] - b[0]) * (point[1] - c[1])) / denominator
    second = ((c[1] - a[1]) * (point[0] - c[0]) + (a[0] - c[0]) * (point[1] - c[1])) / denominator
    third = 1.0 - first - second
    epsilon = -1.0e-7
    if first < epsilon or second < epsilon or third < epsilon:
        return None
    return first, second, third


def bilinear_sample(
    pixels: Sequence[float], width: int, height: int, x: float, y: float, channels: int = 4
) -> tuple[float, float, float, float]:
    """좌하단 원점 픽셀 배열을 bilinear 방식으로 읽는다."""

    if width <= 0 or height <= 0 or channels < 3:
        raise ValueError("이미지 크기 또는 채널 수가 올바르지 않습니다.")
    x = _clamp(x, 0.0, float(width - 1))
    y = _clamp(y, 0.0, float(height - 1))
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    x1, y1 = min(width - 1, x0 + 1), min(height - 1, y0 + 1)
    tx, ty = x - x0, y - y0

    def channel(px: int, py: int, index: int) -> float:
        if index >= channels:
            return 1.0
        return float(pixels[(py * width + px) * channels + index])

    result = []
    for channel_index in range(4):
        bottom = channel(x0, y0, channel_index) * (1.0 - tx) + channel(x1, y0, channel_index) * tx
        top = channel(x0, y1, channel_index) * (1.0 - tx) + channel(x1, y1, channel_index) * tx
        result.append(bottom * (1.0 - ty) + top * ty)
    return tuple(result)  # type: ignore[return-value]


def detect_foreground_bbox(
    pixels: Sequence[float | int], width: int, height: int, channels: int = 4
) -> BBox:
    """투명 또는 단색 계열 배경에서 피사체의 경계 상자를 찾는다.

    반환 좌표의 최대값은 파이썬 slice처럼 exclusive이다. 판별이 불가능하면
    전체 이미지를 반환해 생성 이미지의 일부를 잘못 버리지 않는다.
    """

    if width <= 0 or height <= 0 or channels < 3:
        raise ValueError("이미지 크기 또는 채널 수가 올바르지 않습니다.")
    expected = width * height * channels
    if len(pixels) < expected:
        raise ValueError("픽셀 배열 길이가 이미지 크기보다 짧습니다.")

    alpha_available = channels >= 4
    alpha_values = []
    border_samples: list[tuple[float, float, float, float]] = []
    stride = max(1, min(width, height) // 256)

    def read(px: int, py: int) -> tuple[float, float, float, float]:
        offset = (py * width + px) * channels
        alpha = _as_unit(pixels[offset + 3]) if alpha_available else 1.0
        return (
            _as_unit(pixels[offset]),
            _as_unit(pixels[offset + 1]),
            _as_unit(pixels[offset + 2]),
            alpha,
        )

    for x in range(0, width, stride):
        border_samples.append(read(x, 0))
        border_samples.append(read(x, height - 1))
    for y in range(0, height, stride):
        border_samples.append(read(0, y))
        border_samples.append(read(width - 1, y))
    if alpha_available:
        alpha_values = [sample[3] for sample in border_samples]

    transparent_border = bool(alpha_values) and sum(alpha_values) / len(alpha_values) < 0.25
    sorted_channels = [sorted(sample[channel] for sample in border_samples) for channel in range(4)]
    middle = len(border_samples) // 2
    background = tuple(values[middle] for values in sorted_channels)

    # 투명도를 표현한 checker 배경은 두 색이 번갈아 나타난다. 한 가지 중앙값만
    # 배경으로 보면 나머지 checker 색과 구분선이 이어져 거대한 전경이 될 수
    # 있으므로, 테두리에서 반복되는 지배 색을 최대 네 개까지 배경으로 인정한다.
    color_bins: dict[int, list[float | int]] = {}
    for sample in border_samples:
        red = min(15, int(sample[0] * 16.0))
        green = min(15, int(sample[1] * 16.0))
        blue = min(15, int(sample[2] * 16.0))
        key = (red << 8) | (green << 4) | blue
        bucket = color_bins.setdefault(key, [0, 0.0, 0.0, 0.0])
        bucket[0] += 1
        bucket[1] += sample[0]
        bucket[2] += sample[1]
        bucket[3] += sample[2]
    minimum_palette_count = max(2, int(round(len(border_samples) * 0.04)))
    dominant_bins = sorted(color_bins.values(), key=lambda bucket: int(bucket[0]), reverse=True)
    background_palette = [
        (
            float(bucket[1]) / int(bucket[0]),
            float(bucket[2]) / int(bucket[0]),
            float(bucket[3]) / int(bucket[0]),
        )
        for bucket in dominant_bins[:4]
        if int(bucket[0]) >= minimum_palette_count
    ]
    if not background_palette:
        background_palette = [(background[0], background[1], background[2])]

    segment_start = background_palette[0]
    segment_vector = (0.0, 0.0, 0.0)
    segment_length_squared = 0.0
    if len(background_palette) >= 2:
        segment_vector = tuple(
            background_palette[1][channel] - segment_start[channel] for channel in range(3)
        )
        segment_length_squared = sum(value * value for value in segment_vector)

    def distance_to_background(color: Sequence[float]) -> float:
        """지배 배경색과 checker 경계의 보간색까지 포함한 색 거리를 구한다."""

        distance = min(
            max(abs(color[channel] - palette_color[channel]) for channel in range(3))
            for palette_color in background_palette
        )
        if segment_length_squared <= 1.0e-8:
            return distance
        projection = sum(
            (color[channel] - segment_start[channel]) * segment_vector[channel]
            for channel in range(3)
        ) / segment_length_squared
        projection = _clamp(projection)
        segment_distance = max(
            abs(
                color[channel]
                - (segment_start[channel] + segment_vector[channel] * projection)
            )
            for channel in range(3)
        )
        return min(distance, segment_distance)

    deviations = sorted(
        distance_to_background(sample)
        for sample in border_samples
    )
    median_deviation = deviations[len(deviations) // 2]
    threshold = min(0.20, max(0.055, median_deviation * 4.0 + 0.035))

    # bytearray 마스크와 재사용 가능한 uint32 큐만 사용한다. 생성 이미지가
    # 1K 이상이어도 픽셀마다 파이썬 객체를 만들지 않아 메모리 사용이 안정적이다.
    foreground_mask = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            color = read(x, y)
            if transparent_border:
                foreground = color[3] > 0.20
            else:
                background_distance = distance_to_background(color)
                foreground = color[3] > 0.05 and background_distance > threshold
            if foreground:
                foreground_mask[y * width + x] = 1

    # AI가 3면도에 추가한 FRONT 같은 라벨과 구분선은 본체와 떨어진 작은
    # 연결 성분이다. 모든 전경의 min/max를 쓰지 않고 가장 크면서 화면 중앙에
    # 가까운 성분을 본체로 정한 뒤, 근접한 부품 성분만 합친다.
    components = array("I")
    queue = array("I")
    pixel_count = width * height
    for start in range(pixel_count):
        if foreground_mask[start] != 1:
            continue
        del queue[:]
        queue.append(start)
        foreground_mask[start] = 2
        cursor = 0
        area = 0
        left = right = start % width
        bottom = top = start // width
        while cursor < len(queue):
            index = queue[cursor]
            cursor += 1
            x, y = index % width, index // width
            area += 1
            left = min(left, x)
            right = max(right, x)
            bottom = min(bottom, y)
            top = max(top, y)
            if x > 0 and foreground_mask[index - 1] == 1:
                foreground_mask[index - 1] = 2
                queue.append(index - 1)
            if x + 1 < width and foreground_mask[index + 1] == 1:
                foreground_mask[index + 1] = 2
                queue.append(index + 1)
            if y > 0 and foreground_mask[index - width] == 1:
                foreground_mask[index - width] = 2
                queue.append(index - width)
            if y + 1 < height and foreground_mask[index + width] == 1:
                foreground_mask[index + width] = 2
                queue.append(index + width)
        components.extend((area, left, bottom, right + 1, top + 1))

    if not components:
        return 0, 0, width, height

    def is_separator(component_left: int, component_bottom: int, component_right: int, component_top: int) -> bool:
        """길고 매우 얇은 3면도 구분선을 피사체 후보에서 제외한다."""

        component_width = component_right - component_left
        component_height = component_top - component_bottom
        edge_margin_x = max(1, int(round(width * 0.005)))
        edge_margin_y = max(1, int(round(height * 0.005)))
        vertical = (
            component_width <= max(3, int(round(width * 0.03)))
            and component_bottom <= edge_margin_y
            and component_top >= height - edge_margin_y
        )
        horizontal = (
            component_height <= max(3, int(round(height * 0.03)))
            and component_left <= edge_margin_x
            and component_right >= width - edge_margin_x
        )
        return vertical or horizontal

    main_offset = -1
    main_score = -1.0
    fallback_offset = 0
    fallback_area = -1
    for offset in range(0, len(components), 5):
        area, left, bottom, right, top = components[offset : offset + 5]
        if area > fallback_area:
            fallback_area = area
            fallback_offset = offset
        if is_separator(left, bottom, right, top):
            continue
        center_x = (left + right) * 0.5 / width
        center_y = (bottom + top) * 0.5 / height
        center_distance = min(1.0, math.hypot(center_x - 0.5, center_y - 0.5) / math.sqrt(0.5))
        score = area * (1.0 - center_distance * 0.35)
        if score > main_score:
            main_score = score
            main_offset = offset

    if main_offset < 0:
        main_offset = fallback_offset
    main_area, left, bottom, right, top = components[main_offset : main_offset + 5]
    main_width = right - left
    main_height = top - bottom
    close_gap = max(2, int(round(min(main_width, main_height) * 0.06)))
    accessory_gap = max(close_gap, int(round(max(main_width, main_height) * 0.20)))

    for offset in range(0, len(components), 5):
        if offset == main_offset:
            continue
        area, candidate_left, candidate_bottom, candidate_right, candidate_top = components[offset : offset + 5]
        if is_separator(candidate_left, candidate_bottom, candidate_right, candidate_top):
            continue
        gap_x = max(0, left - candidate_right, candidate_left - right)
        gap_y = max(0, bottom - candidate_top, candidate_bottom - top)
        gap = max(gap_x, gap_y)
        area_ratio = area / max(1, main_area)
        # 작은 손·장식은 본체 바로 곁에 있을 때 유지하고, 검·방패처럼 큰
        # 분리 부품은 조금 더 떨어져 있어도 포함한다. 멀리 놓인 글자는 제외된다.
        if (area_ratio >= 0.001 and gap <= close_gap) or (area_ratio >= 0.02 and gap <= accessory_gap):
            left = min(left, candidate_left)
            bottom = min(bottom, candidate_bottom)
            right = max(right, candidate_right)
            top = max(top, candidate_top)

    padding_x = max(1, int(round(width * 0.005)))
    padding_y = max(1, int(round(height * 0.005)))
    return (
        max(0, left - padding_x),
        max(0, bottom - padding_y),
        min(width, right + padding_x),
        min(height, top + padding_y),
    )


def dilate_rgba(rgba: bytearray, occupied: bytearray, width: int, height: int, padding: int) -> int:
    """UV 경계색을 빈 픽셀로 확장해 mipmap seam을 줄인다."""

    pixel_count = width * height
    if len(rgba) != pixel_count * 4 or len(occupied) != pixel_count:
        raise ValueError("Atlas 픽셀 버퍼 크기가 올바르지 않습니다.")
    if padding <= 0:
        return 0
    padding = min(65534, int(padding))
    distances = array("H", [65535]) * pixel_count
    queue: deque[int] = deque()
    for index, value in enumerate(occupied):
        if value:
            distances[index] = 0
            queue.append(index)

    expanded = 0
    while queue:
        index = queue.popleft()
        distance = distances[index]
        if distance >= padding:
            continue
        x, y = index % width, index // width
        neighbors = []
        if x > 0:
            neighbors.append(index - 1)
        if x + 1 < width:
            neighbors.append(index + 1)
        if y > 0:
            neighbors.append(index - width)
        if y + 1 < height:
            neighbors.append(index + width)
        for neighbor in neighbors:
            if distances[neighbor] != 65535:
                continue
            distances[neighbor] = distance + 1
            source_offset = index * 4
            target_offset = neighbor * 4
            rgba[target_offset : target_offset + 4] = rgba[source_offset : source_offset + 4]
            occupied[neighbor] = 2
            expanded += 1
            queue.append(neighbor)
    return expanded


def _png_chunk(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", binascii.crc32(name + payload) & 0xFFFFFFFF)


def encode_srgb_png(rgba: bytes | bytearray, width: int, height: int) -> bytes:
    """좌하단 원점 RGBA8 버퍼를 표준 sRGB PNG로 인코딩한다."""

    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise ValueError("PNG로 저장할 픽셀 버퍼 크기가 올바르지 않습니다.")
    rows = bytearray()
    row_bytes = width * 4
    for y in range(height - 1, -1, -1):
        rows.append(0)  # PNG filter None
        start = y * row_bytes
        rows.extend(rgba[start : start + row_bytes])
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            _png_chunk(b"IHDR", header),
            _png_chunk(b"sRGB", b"\x00"),
            _png_chunk(b"gAMA", struct.pack(">I", 45455)),
            _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=6)),
            _png_chunk(b"IEND", b""),
        )
    )


def _linear_to_srgb_byte(value: float) -> int:
    value = _clamp(value)
    if value <= 0.0031308:
        encoded = value * 12.92
    else:
        encoded = 1.055 * (value ** (1.0 / 2.4)) - 0.055
    return int(round(_clamp(encoded) * 255.0))


def _srgb_to_linear(value: float) -> float:
    """Blender가 반환한 파일 픽셀의 sRGB 채널을 선형값으로 바꾼다."""

    value = _clamp(float(value))
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _projected_bbox(triangles: Sequence[BakeTriangle], view: str, center: Vec3, scale: float) -> tuple[float, float, float, float]:
    projected = [project_point(point, view, center, scale) for triangle in triangles for point in triangle.positions]
    minimum_u = min(point[0] for point in projected)
    maximum_u = max(point[0] for point in projected)
    minimum_v = min(point[1] for point in projected)
    maximum_v = max(point[1] for point in projected)
    epsilon = 1.0e-8
    if maximum_u - minimum_u < epsilon:
        minimum_u, maximum_u = 0.0, 1.0
    if maximum_v - minimum_v < epsilon:
        minimum_v, maximum_v = 0.0, 1.0
    return minimum_u, minimum_v, maximum_u, maximum_v


def _screen_raster_bounds(points: tuple[Vec2, Vec2, Vec2], width: int, height: int) -> tuple[int, int, int, int]:
    minimum_x = max(0, int(math.floor(min(point[0] for point in points) * width - 0.5)))
    maximum_x = min(width - 1, int(math.ceil(max(point[0] for point in points) * width - 0.5)))
    minimum_y = max(0, int(math.floor(min(point[1] for point in points) * height - 0.5)))
    maximum_y = min(height - 1, int(math.ceil(max(point[1] for point in points) * height - 0.5)))
    return minimum_x, minimum_y, maximum_x, maximum_y


def build_depth_buffer(
    triangles: Sequence[BakeTriangle], view: str, center: Vec3, scale: float, resolution: int
) -> array:
    """화면 픽셀마다 카메라에 가장 가까운 depth(max)를 기록한다."""

    if resolution <= 0:
        raise ValueError("depth buffer 해상도는 0보다 커야 합니다.")
    buffer = array("f", [-math.inf]) * (resolution * resolution)
    for triangle in triangles:
        projected = tuple(project_point(point, view, center, scale) for point in triangle.positions)
        screen = tuple((point[0], point[1]) for point in projected)
        bounds = _screen_raster_bounds(screen, resolution, resolution)
        if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
            continue
        for y in range(bounds[1], bounds[3] + 1):
            py = (y + 0.5) / resolution
            for x in range(bounds[0], bounds[2] + 1):
                px = (x + 0.5) / resolution
                weights = barycentric_weights((px, py), screen[0], screen[1], screen[2])
                if weights is None:
                    continue
                depth = sum(weights[index] * projected[index][2] for index in range(3))
                pixel_index = y * resolution + x
                if depth > buffer[pixel_index]:
                    buffer[pixel_index] = depth
    return buffer


def _visible_in_depth(
    projected: tuple[float, float, float], depth_buffer: Sequence[float], resolution: int, tolerance: float
) -> bool:
    u, v, depth = projected
    if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
        return False
    x = min(resolution - 1, max(0, int(u * resolution)))
    y = min(resolution - 1, max(0, int(v * resolution)))
    nearest = -math.inf
    for offset_y in (-1, 0, 1):
        sample_y = y + offset_y
        if sample_y < 0 or sample_y >= resolution:
            continue
        for offset_x in (-1, 0, 1):
            sample_x = x + offset_x
            if 0 <= sample_x < resolution:
                nearest = max(nearest, float(depth_buffer[sample_y * resolution + sample_x]))
    return math.isfinite(nearest) and depth >= nearest - tolerance


def _is_foreground(source: RasterSource, color: tuple[float, float, float, float]) -> bool:
    if source.background[3] < 0.25:
        return color[3] > 0.12
    difference = max(abs(color[channel] - source.background[channel]) for channel in range(3))
    return color[3] > 0.05 and difference > source.background_threshold * 0.55


def _aligned_source_sample(
    source: RasterSource,
    projected: tuple[float, float, float],
    projected_bbox: tuple[float, float, float, float],
    *,
    mirror_x: bool = False,
    vertical_fallback: int = 0,
) -> tuple[float, float, float, float] | None:
    u, v = projected[0], projected[1]
    min_u, min_v, max_u, max_v = projected_bbox
    normalized_u = _clamp((u - min_u) / max(1.0e-8, max_u - min_u))
    normalized_v = _clamp((v - min_v) / max(1.0e-8, max_v - min_v))
    if mirror_x:
        normalized_u = 1.0 - normalized_u
    if vertical_fallback > 0:
        normalized_v = min(normalized_v, 0.94)
    elif vertical_fallback < 0:
        normalized_v = max(normalized_v, 0.06)
    left, bottom, right, top = source.subject_bbox
    target_x = left + normalized_u * max(0, right - left - 1)
    target_y = bottom + normalized_v * max(0, top - bottom - 1)
    center_x = (left + right - 1) * 0.5
    center_y = (bottom + top - 1) * 0.5
    for amount in (0.0, 0.035, 0.075, 0.13):
        x = target_x * (1.0 - amount) + center_x * amount
        y = target_y * (1.0 - amount) + center_y * amount
        color = bilinear_sample(source.pixels, source.width, source.height, x, y)
        if _is_foreground(source, color):
            return color
    return None


def _normal_view_weights(normal: Vec3) -> dict[str, float]:
    x, y, _z = normal
    return {
        "FRONT": max(0.0, -y) ** 2,
        "RIGHT": max(0.0, x) ** 2,
        "BACK": max(0.0, y) ** 2,
        "LEFT": max(0.0, -x) ** 2,
    }


def rasterize_atlas(
    triangles: Sequence[BakeTriangle],
    sources: Mapping[str, RasterSource],
    resolution: int,
    padding: int,
    center: Vec3,
    scale: float,
) -> tuple[bytearray, dict[str, int]]:
    """삼각형을 UV 공간에 래스터화하고 생성 뷰 색을 투영한다."""

    if not triangles:
        raise ValueError("Atlas에 투영할 삼각형이 없습니다.")
    if resolution < 16 or resolution > MAX_ATLAS_RESOLUTION:
        raise ValueError(
            f"텍스처 해상도는 16~{MAX_ATLAS_RESOLUTION}px만 지원합니다. "
            "8192px가 필요하면 우선 4096px로 생성한 뒤 업스케일해 주세요."
        )
    missing = [name for name in SOURCE_VIEW_NAMES if name not in sources]
    if missing:
        raise ValueError(f"생성 뷰 이미지가 없습니다: {', '.join(missing)}")

    projected_bboxes = {name: _projected_bbox(triangles, name, center, scale) for name in VIEW_NAMES}
    source_limit = max(max(source.width, source.height) for source in sources.values())
    depth_resolution = min(1024, max(256, min(resolution, source_limit)))
    depth_buffers = {
        name: build_depth_buffer(triangles, name, center, scale, depth_resolution)
        for name in VIEW_NAMES
    }
    depth_tolerance = max(scale / depth_resolution * 3.0, scale * 1.0e-4)
    rgba = bytearray(resolution * resolution * 4)
    occupied = bytearray(resolution * resolution)
    filled_pixels = 0
    occluded_samples = 0
    fallback_pixels = 0

    for triangle in triangles:
        uv_screen = tuple((uv[0], uv[1]) for uv in triangle.uvs)
        bounds = _screen_raster_bounds(uv_screen, resolution, resolution)
        if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
            continue
        view_weights = _normal_view_weights(triangle.normal)
        vertical = abs(triangle.normal[2]) >= max(abs(triangle.normal[0]), abs(triangle.normal[1]))
        if vertical:
            view_weights = {name: 1.0 for name in VIEW_NAMES}
        for y in range(bounds[1], bounds[3] + 1):
            uv_y = (y + 0.5) / resolution
            for x in range(bounds[0], bounds[2] + 1):
                pixel_index = y * resolution + x
                if occupied[pixel_index] == 1:
                    continue
                uv_x = (x + 0.5) / resolution
                weights = barycentric_weights((uv_x, uv_y), uv_screen[0], uv_screen[1], uv_screen[2])
                if weights is None:
                    continue
                position = tuple(
                    sum(weights[index] * triangle.positions[index][axis] for index in range(3))
                    for axis in range(3)
                )
                colors: list[tuple[tuple[float, float, float, float], float]] = []
                for view, view_weight in view_weights.items():
                    if view_weight <= 1.0e-8:
                        continue
                    projected = project_point(position, view, center, scale)
                    visible = _visible_in_depth(
                        projected, depth_buffers[view], depth_resolution, depth_tolerance
                    )
                    if not visible and not vertical:
                        occluded_samples += 1
                        continue
                    source_name = "RIGHT" if view == "LEFT" else view
                    source = sources[source_name]
                    vertical_fallback = 0
                    if vertical:
                        vertical_fallback = 1 if triangle.normal[2] > 0.0 else -1
                    color = _aligned_source_sample(
                        source,
                        projected,
                        projected_bboxes[view],
                        mirror_x=view == "LEFT",
                        vertical_fallback=vertical_fallback,
                    )
                    if color is not None:
                        colors.append((color, view_weight))
                if not colors:
                    fallback_pixels += 1
                    available = [
                        (sources["FRONT"].fallback_color, max(0.001, view_weights["FRONT"])),
                        (sources["RIGHT"].fallback_color, max(0.001, view_weights["RIGHT"] + view_weights["LEFT"])),
                        (sources["BACK"].fallback_color, max(0.001, view_weights["BACK"])),
                    ]
                    colors = available
                total_weight = sum(item[1] for item in colors)
                color = tuple(
                    sum(item[0][channel] * item[1] for item in colors) / total_weight
                    for channel in range(4)
                )
                offset = pixel_index * 4
                rgba[offset] = _linear_to_srgb_byte(color[0])
                rgba[offset + 1] = _linear_to_srgb_byte(color[1])
                rgba[offset + 2] = _linear_to_srgb_byte(color[2])
                rgba[offset + 3] = 255
                occupied[pixel_index] = 1
                filled_pixels += 1

    if filled_pixels == 0:
        raise ValueError("UV가 0~1 Atlas 영역에 없어 텍스처를 만들 수 없습니다.")
    dilated_pixels = dilate_rgba(rgba, occupied, resolution, resolution, padding)
    return rgba, {
        "filled_pixels": filled_pixels,
        "dilated_pixels": dilated_pixels,
        "occluded_samples": occluded_samples,
        "fallback_pixels": fallback_pixels,
        "depth_resolution": depth_resolution,
    }


def _source_statistics(pixels: Sequence[float], width: int, height: int, bbox: BBox) -> tuple[
    tuple[float, float, float, float], float, tuple[float, float, float, float]
]:
    border: list[tuple[float, float, float, float]] = []
    stride = max(1, min(width, height) // 256)

    def read(x: int, y: int) -> tuple[float, float, float, float]:
        offset = (y * width + x) * 4
        return tuple(float(pixels[offset + channel]) for channel in range(4))  # type: ignore[return-value]

    for x in range(0, width, stride):
        border.extend((read(x, 0), read(x, height - 1)))
    for y in range(0, height, stride):
        border.extend((read(0, y), read(width - 1, y)))
    background_values = []
    for channel in range(4):
        values = sorted(color[channel] for color in border)
        background_values.append(values[len(values) // 2])
    background = tuple(background_values)  # type: ignore[assignment]
    deviations = sorted(
        max(abs(color[channel] - background[channel]) for channel in range(3))
        for color in border
    )
    threshold = min(0.30, max(0.055, deviations[len(deviations) // 2] * 4.0 + 0.035))

    left, bottom, right, top = bbox
    sample_step = max(1, int(math.sqrt(max(1, (right - left) * (top - bottom) // 20000))))
    totals = [0.0, 0.0, 0.0, 0.0]
    count = 0
    for y in range(bottom, top, sample_step):
        for x in range(left, right, sample_step):
            color = read(x, y)
            if background[3] < 0.25:
                foreground = color[3] > 0.12
            else:
                foreground = max(abs(color[channel] - background[channel]) for channel in range(3)) > threshold * 0.55
            if foreground:
                for channel in range(4):
                    totals[channel] += color[channel]
                count += 1
    fallback = tuple(value / count for value in totals) if count else (0.5, 0.5, 0.5, 1.0)
    return background, threshold, fallback  # type: ignore[return-value]


def _resolve_view_paths(view_paths: Mapping[str, str | os.PathLike[str]] | Sequence[str | os.PathLike[str]]) -> dict[str, Path]:
    if isinstance(view_paths, Mapping):
        resolved = {str(name).upper(): Path(path).expanduser().resolve() for name, path in view_paths.items()}
    else:
        if len(view_paths) != 3:
            raise ValueError("FRONT/RIGHT/BACK 생성 이미지 3장이 필요합니다.")
        resolved = {name: Path(path).expanduser().resolve() for name, path in zip(SOURCE_VIEW_NAMES, view_paths)}
    missing = [name for name in SOURCE_VIEW_NAMES if name not in resolved]
    if missing:
        raise ValueError(f"생성 뷰 경로가 없습니다: {', '.join(missing)}")
    for name in SOURCE_VIEW_NAMES:
        path = resolved[name]
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{name} 생성 이미지를 읽을 수 없습니다: {path}")
    return {name: resolved[name] for name in SOURCE_VIEW_NAMES}


def _resolve_uv_name(obj, uv_layer_names, object_index: int) -> str:
    if isinstance(uv_layer_names, Mapping):
        name = uv_layer_names.get(obj.name) or uv_layer_names.get(obj)
    elif uv_layer_names is not None:
        try:
            name = uv_layer_names[object_index]
        except IndexError as exc:
            raise ValueError(f"{obj.name}: 객체별 UV 레이어 이름이 부족합니다.") from exc
    else:
        active = obj.data.uv_layers.active
        name = active.name if active is not None else ""
    if not name:
        raise ValueError(f"{obj.name}: 사용할 UV 레이어가 없습니다.")
    return str(name)


def _topology_matches(original, evaluated) -> bool:
    if (
        len(original.vertices) != len(evaluated.vertices)
        or len(original.loops) != len(evaluated.loops)
        or len(original.polygons) != len(evaluated.polygons)
    ):
        return False
    if any(left.vertex_index != right.vertex_index for left, right in zip(original.loops, evaluated.loops)):
        return False
    return all(
        left.loop_start == right.loop_start and left.loop_total == right.loop_total
        for left, right in zip(original.polygons, evaluated.polygons)
    )


def _collect_blender_triangles(context, objects: Sequence, uv_layer_names) -> tuple[list[BakeTriangle], Vec3, float]:
    depsgraph = context.evaluated_depsgraph_get()
    selected = set(objects)
    selected_mesh_owners = {}
    for obj in objects:
        previous = selected_mesh_owners.get(obj.data)
        if previous is not None and previous.matrix_world != obj.matrix_world:
            raise ValueError(
                f"{obj.data.name}: 서로 다른 Transform의 선택 객체가 같은 Mesh와 UV를 공유합니다. "
                "Single User로 분리한 뒤 다시 실행해 주세요."
            )
        selected_mesh_owners[obj.data] = obj
    for mesh in {obj.data for obj in objects}:
        outsiders = [
            obj.name
            for obj in bpy.data.objects
            if obj.type == "MESH" and obj.data is mesh and obj not in selected
        ]
        if outsiders:
            raise ValueError(
                f"{mesh.name}: 선택 밖의 linked Mesh 인스턴스({', '.join(outsiders[:3])})가 있습니다. "
                "Single User로 분리한 뒤 다시 실행해 주세요."
            )

    bounds_points = []
    triangles: list[BakeTriangle] = []
    for object_index, obj in enumerate(objects):
        if obj.mode != "OBJECT":
            raise ValueError(f"{obj.name}: Object Mode에서 텍스처를 적용해 주세요.")
        evaluated_object = obj.evaluated_get(depsgraph)
        bounds_points.extend(
            tuple(evaluated_object.matrix_world @ Vector(corner))
            for corner in evaluated_object.bound_box
        )
        evaluated_mesh = evaluated_object.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            if not _topology_matches(obj.data, evaluated_mesh):
                raise ValueError(
                    f"{obj.name}: Modifier 평가 결과가 원본 polygon/loop topology와 다릅니다. "
                    "Modifier를 적용하거나 비활성화한 뒤 다시 실행해 주세요."
                )
            uv_name = _resolve_uv_name(obj, uv_layer_names, object_index)
            uv_layer = evaluated_mesh.uv_layers.get(uv_name)
            if uv_layer is None:
                raise ValueError(f"{obj.name}: 평가 Mesh에 '{uv_name}' UV 레이어가 없습니다.")
            evaluated_mesh.calc_loop_triangles()
            matrix = evaluated_object.matrix_world
            normal_matrix = matrix.to_3x3().inverted_safe().transposed()
            for loop_triangle in evaluated_mesh.loop_triangles:
                polygon = evaluated_mesh.polygons[loop_triangle.polygon_index]
                normal_vector = (normal_matrix @ polygon.normal).normalized()
                positions = tuple(
                    tuple(matrix @ evaluated_mesh.vertices[vertex_index].co)
                    for vertex_index in loop_triangle.vertices
                )
                uvs = tuple(tuple(uv_layer.data[loop_index].uv) for loop_index in loop_triangle.loops)
                triangles.append(
                    BakeTriangle(
                        positions=positions,  # type: ignore[arg-type]
                        uvs=uvs,  # type: ignore[arg-type]
                        normal=tuple(normal_vector),
                    )
                )
        finally:
            evaluated_object.to_mesh_clear()

    if not bounds_points or not triangles:
        raise ValueError("투영할 Mesh 삼각형이 없습니다.")
    minimum = tuple(min(point[axis] for point in bounds_points) for axis in range(3))
    maximum = tuple(max(point[axis] for point in bounds_points) for axis in range(3))
    center = tuple((minimum[axis] + maximum[axis]) * 0.5 for axis in range(3))
    extent = tuple(maximum[axis] - minimum[axis] for axis in range(3))
    scale = max(extent[0], extent[1], extent[2], 0.01) * 1.2
    return triangles, center, scale  # type: ignore[return-value]


def _load_raster_sources(paths: Mapping[str, Path]) -> tuple[dict[str, RasterSource], list]:
    sources: dict[str, RasterSource] = {}
    loaded_images = []
    try:
        for name in SOURCE_VIEW_NAMES:
            image = bpy.data.images.load(str(paths[name]), check_existing=False)
            loaded_images.append(image)
            width, height = map(int, image.size)
            if width <= 0 or height <= 0:
                raise ValueError(f"{name} 생성 이미지 크기가 올바르지 않습니다.")
            pixels = array("f", [0.0]) * (width * height * 4)
            image.pixels.foreach_get(pixels)
            # Blender 5.2의 Image.pixels는 sRGB PNG/JPEG를 읽을 때도 파일의
            # 인코딩된 RGB 값(예: 128 -> 0.501961)을 반환한다. 투영 뷰를 이
            # 상태로 블렌딩한 뒤 다시 sRGB 인코딩하면 중간톤이 이중 감마로
            # 밝아지므로, 소스 RGB만 명시적으로 선형화한다.
            for offset in range(0, len(pixels), 4):
                pixels[offset] = _srgb_to_linear(pixels[offset])
                pixels[offset + 1] = _srgb_to_linear(pixels[offset + 1])
                pixels[offset + 2] = _srgb_to_linear(pixels[offset + 2])
            bbox = detect_foreground_bbox(pixels, width, height)
            background, threshold, fallback = _source_statistics(pixels, width, height, bbox)
            if bbox == (0, 0, width, height):
                # 피사체가 프레임을 가득 채우면 border와의 차이를 요구하지 않는다.
                threshold = -1.0
            sources[name] = RasterSource(
                width=width,
                height=height,
                pixels=pixels,
                subject_bbox=bbox,
                background=background,
                background_threshold=threshold,
                fallback_color=fallback,
            )
        return sources, loaded_images
    except Exception:
        for image in loaded_images:
            bpy.data.images.remove(image)
        raise


def _find_reusable_image(output_path: Path):
    normalized = os.path.normcase(str(output_path.resolve()))
    for image in bpy.data.images:
        if not image.get(_IMAGE_MARKER):
            continue
        marked_path = image.get("uvmapping_output_path", "")
        if marked_path and os.path.normcase(str(Path(marked_path).resolve())) == normalized:
            return image
    return None


def _find_reusable_material(output_path: Path):
    normalized = os.path.normcase(str(output_path.resolve()))
    for material in bpy.data.materials:
        if not material.get(_MATERIAL_MARKER):
            continue
        marked_path = material.get("uvmapping_output_path", "")
        if marked_path and os.path.normcase(str(Path(marked_path).resolve())) == normalized:
            return material
    return None


def _configure_material(material, image, output_path: Path) -> None:
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    output = next((node for node in nodes if node.bl_idname == "ShaderNodeOutputMaterial"), None)
    if output is None:
        output = nodes.new("ShaderNodeOutputMaterial")
    principled = next((node for node in nodes if node.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    if principled is None:
        principled = nodes.new("ShaderNodeBsdfPrincipled")
    texture = next((node for node in nodes if node.get(_IMAGE_MARKER)), None)
    if texture is None:
        texture = nodes.new("ShaderNodeTexImage")
        texture[_IMAGE_MARKER] = True
        texture.label = "UVMapping AI Albedo"
    texture.image = image
    texture.interpolation = "Linear"
    for link in tuple(principled.inputs["Base Color"].links):
        links.remove(link)
    links.new(texture.outputs["Color"], principled.inputs["Base Color"])
    if "Roughness" in principled.inputs:
        principled.inputs["Roughness"].default_value = 0.82
    for link in tuple(output.inputs["Surface"].links):
        links.remove(link)
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    material[_MATERIAL_MARKER] = True
    material["uvmapping_output_path"] = str(output_path)


def _apply_material_transaction(objects: Sequence, material) -> tuple[dict, list, dict]:
    mesh_snapshots = {}
    appended_meshes = []
    active_indices = {obj: obj.active_material_index for obj in objects}
    try:
        material_indices = {}
        for obj in objects:
            mesh = obj.data
            if mesh not in mesh_snapshots:
                mesh_snapshots[mesh] = tuple(polygon.material_index for polygon in mesh.polygons)
                material_index = mesh.materials.find(material.name)
                if material_index < 0:
                    mesh.materials.append(material)
                    material_index = len(mesh.materials) - 1
                    appended_meshes.append(mesh)
                material_indices[mesh] = material_index
                for polygon in mesh.polygons:
                    polygon.material_index = material_index
            obj.active_material_index = material_indices[mesh]
        return mesh_snapshots, appended_meshes, active_indices
    except Exception:
        _rollback_material_application(mesh_snapshots, appended_meshes, material, active_indices)
        raise


def _rollback_material_application(
    mesh_snapshots: Mapping, appended_meshes: Sequence, material, active_indices: Mapping | None = None
) -> None:
    for mesh, indices in mesh_snapshots.items():
        for polygon, material_index in zip(mesh.polygons, indices):
            polygon.material_index = material_index
    for mesh in reversed(appended_meshes):
        if mesh.materials and mesh.materials[-1] == material:
            mesh.materials.pop(index=len(mesh.materials) - 1)
    for obj, active_index in (active_indices or {}).items():
        if obj.name in bpy.data.objects:
            obj.active_material_index = min(active_index, max(0, len(obj.material_slots) - 1))


def bake_diffuse(
    context,
    objects: Sequence,
    view_paths: Mapping[str, str | os.PathLike[str]] | Sequence[str | os.PathLike[str]],
    output_path: str | os.PathLike[str],
    resolution: int,
    padding: int,
    uv_layer_names=None,
) -> dict:
    """FRONT/RIGHT/BACK 생성 뷰를 공유 UV Atlas에 굽고 재질에 연결한다.

    Atlas 파일이 완전히 만들어지기 전에는 Blender 재질/이미지 상태를 바꾸지
    않는다. 파일 교체와 재질 적용 중 실패하면 가능한 범위에서 이전 상태를
    복원한다.
    """

    if bpy is None:
        raise RuntimeError("bake_diffuse는 Blender 안에서만 실행할 수 있습니다.")
    targets = tuple(objects)
    if not targets or any(obj is None or obj.type != "MESH" for obj in targets):
        raise ValueError("텍스처를 적용할 Mesh 객체가 필요합니다.")
    resolution = int(resolution)
    padding = max(0, int(padding))
    if resolution > MAX_ATLAS_RESOLUTION:
        raise ValueError(
            "현재 CPU Atlas 베이크는 최대 4096px까지 지원합니다. "
            "8192px 설정은 4096px로 낮춘 뒤 다시 실행해 주세요."
        )
    paths = _resolve_view_paths(view_paths)
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".png":
        raise ValueError("Diffuse/Albedo 출력 경로는 .png여야 합니다.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    triangles, center, scale = _collect_blender_triangles(context, targets, uv_layer_names)
    sources, loaded_images = _load_raster_sources(paths)
    stage_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp.png")
    backup_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.bak")
    image = None
    material = None
    created_image = False
    created_material = False
    mesh_snapshots = {}
    appended_meshes = []
    active_indices = {}
    file_committed = False
    try:
        rgba, metrics = rasterize_atlas(
            triangles,
            sources,
            resolution,
            padding,
            center,
            scale,
        )
        stage_path.write_bytes(encode_srgb_png(rgba, resolution, resolution))

        if destination.exists():
            shutil.copy2(destination, backup_path)
        os.replace(stage_path, destination)
        file_committed = True

        image = _find_reusable_image(destination)
        if image is None:
            image = bpy.data.images.load(str(destination), check_existing=False)
            created_image = True
        else:
            image.filepath = str(destination)
            image.reload()
        image.name = f"UVMapping Albedo · {destination.stem}"
        image[_IMAGE_MARKER] = True
        image["uvmapping_output_path"] = str(destination)
        try:
            image.colorspace_settings.name = "sRGB"
        except TypeError:
            pass

        material = _find_reusable_material(destination)
        if material is None:
            material = bpy.data.materials.new(f"UVMapping Albedo · {destination.stem}")
            created_material = True
        _configure_material(material, image, destination)
        mesh_snapshots, appended_meshes, active_indices = _apply_material_transaction(targets, material)

        if backup_path.exists():
            backup_path.unlink()
        return {
            "status": "ALBEDO_APPLIED",
            "output_path": str(destination),
            "image_name": image.name,
            "material_name": material.name,
            "object_names": tuple(obj.name for obj in targets),
            "resolution": resolution,
            "padding": padding,
            "view_names": SOURCE_VIEW_NAMES,
            **metrics,
        }
    except Exception:
        _rollback_material_application(mesh_snapshots, appended_meshes, material, active_indices)
        if created_material and material is not None:
            bpy.data.materials.remove(material)
        if created_image and image is not None:
            bpy.data.images.remove(image)
        if file_committed:
            if backup_path.exists():
                os.replace(backup_path, destination)
                if image is not None and not created_image:
                    try:
                        image.reload()
                    except RuntimeError:
                        pass
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        stage_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        for loaded_image in loaded_images:
            if loaded_image.name in bpy.data.images:
                bpy.data.images.remove(loaded_image)


__all__ = (
    "BakeTriangle",
    "MAX_ATLAS_RESOLUTION",
    "RasterSource",
    "SOURCE_VIEW_NAMES",
    "VIEW_NAMES",
    "bake_diffuse",
    "barycentric_weights",
    "bilinear_sample",
    "build_depth_buffer",
    "detect_foreground_bbox",
    "dilate_rgba",
    "encode_srgb_png",
    "project_point",
    "rasterize_atlas",
)
