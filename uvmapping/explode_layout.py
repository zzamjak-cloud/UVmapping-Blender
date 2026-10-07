"""매핑 파츠를 시점마다 서로 가리지 않게 벌려 놓는 분해도(exploded view) 배치.

전신을 한 번에 생성하면 팔에 가린 옆구리, 다른 다리에 가린 다리 안쪽처럼 어떤
시점에서도 보이지 않는 면이 생긴다. 등록 파츠를 시점마다 화면 평면 안에서만
옮겨 실루엣이 겹치지 않게 하면, 한 장의 생성 그림에 모든 파츠 면이 드러난다.

이동은 화면 평면 축 하나로만 한다. 정면·측면·뒷면은 가로(``right``)로만 옮겨
높이를 그대로 두므로 벨트·소매 끝처럼 둘레를 감싸는 무늬가 시점 사이에서 같은
높이로 이어진다. 위·아래 시점은 높이 축이 없으므로 화면 세로(``up``)로 옮긴다.

이 모듈은 ``bpy`` 없이 동작해 순수 테스트로 검증한다. 결과는 시점 → 파츠별 화면
평면 이동량(월드 단위, ``(right 성분, up 성분)``)이며, 가이드 렌더·베이크·검증이
모두 같은 값을 써야 생성 그림과 모델 투영이 맞는다.
"""

from __future__ import annotations

from collections import deque
import math
from typing import Iterable, Mapping, Sequence

from .texture_bake import VIEW_SPECS


Vec3 = tuple[float, float, float]
Triangle = tuple[Vec3, Vec3, Vec3]

# 실루엣 겹침을 따지는 격자 한 변의 칸 수(시점 안 모델 긴 변 기준).
EXPLODE_GRID = 160
# 서로 떨어뜨린 파츠 사이에 둘 빈틈(모델 긴 변 대비). 3%(사람 크기에서 약 6cm)로 두었을 때
# AI가 측면의 팔을 몸통에 다시 붙여 그리는 일이 실측으로 나와, 조각이 분명히 떨어져 보이게 넓힌다.
EXPLODE_GAP_RATIO = 0.08
# 이어진 파츠가 제자리에서 이 비율(작은 쪽 면적 대비)까지 겹치면 연결부 겹침으로 보고 둔다.
# 손목·발목처럼 맞닿은 경계는 정면에서 작은 쪽의 15~20%가 겹치는 것이 정상이고(실측),
# 옮기면 손발이 팔다리에서 떨어져 나간다. 측면에서 팔이 몸통을 가리는 겹침은 50%를 넘는다.
EXPLODE_ATTACH_TOLERANCE = 0.25
# 이어지지 않은 파츠가 제자리에서 이 비율까지 겹치면 그대로 둔다. 엉덩이 옆에 늘어진
# 손처럼 가장자리만 스치는 경우까지 떼어 내면 손이 팔에서 떨어져 배치가 흐트러진다.
EXPLODE_TOUCH_TOLERANCE = 0.03
# 한 파츠를 옮겨 볼 최대 거리(격자 칸, 모델 긴 변의 배수).
_MAX_SHIFT_SPANS = 3

# 높이 축이 없는 시점은 화면 세로로 옮긴다.
_VERTICAL_SHIFT_VIEWS = frozenset({"TOP", "BOTTOM"})


def _dot(point: Vec3, axis: Vec3) -> float:
    return point[0] * axis[0] + point[1] * axis[1] + point[2] * axis[2]


def triangle_area(triangle: Triangle) -> float:
    """3D 삼각형 넓이."""

    a, b, c = triangle
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


def _shift_axes(view: str) -> tuple[Vec3, Vec3, bool]:
    """(옮기는 축, 고정 축, 세로 이동 여부)."""

    spec = VIEW_SPECS[view]
    if view in _VERTICAL_SHIFT_VIEWS:
        return spec.up, spec.right, True
    return spec.right, spec.up, False


def _rasterize(
    triangles: Sequence[Triangle],
    shift_axis: Vec3,
    fixed_axis: Vec3,
    origin: tuple[float, float],
    cell: float,
) -> dict[int, int]:
    """삼각형들을 행(고정 축) → 열 비트열(옮기는 축) 마스크로 그린다.

    칸 중심이 삼각형 안에 있거나 꼭짓점이 든 칸을 칠한다. 칸보다 가는 삼각형도
    빠지지 않게 하려는 것이다.
    """

    rows: dict[int, int] = {}
    for triangle in triangles:
        points = [
            ((_dot(point, shift_axis) - origin[0]) / cell, (_dot(point, fixed_axis) - origin[1]) / cell)
            for point in triangle
        ]
        for column, row in points:
            key = int(math.floor(row))
            rows[key] = rows.get(key, 0) | (1 << int(math.floor(column)))
        (ax, ay), (bx, by), (cx, cy) = points
        denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denominator) < 1.0e-12:
            continue
        min_row = int(math.floor(min(ay, by, cy)))
        max_row = int(math.floor(max(ay, by, cy)))
        min_column = int(math.floor(min(ax, bx, cx)))
        max_column = int(math.floor(max(ax, bx, cx)))
        for row in range(min_row, max_row + 1):
            y = row + 0.5
            bits = 0
            for column in range(min_column, max_column + 1):
                x = column + 0.5
                first = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / denominator
                second = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / denominator
                if first >= 0.0 and second >= 0.0 and first + second <= 1.0:
                    bits |= 1 << column
            if bits:
                rows[row] = rows.get(row, 0) | bits
    return rows


def assign_unregistered_faces(
    face_vertices: Sequence[Sequence[int]], assignment: Sequence[int], group_count: int
) -> tuple[list[int], int]:
    """파츠에 속하지 않은 면(-1)을 정점으로 이어진 파츠에 붙인다.

    발바닥처럼 사용자가 파츠에 넣지 않은 면을 따로 두면 그 조각만 엉뚱한 곳으로
    밀려난다. 등록 면에서 너비 우선으로 번져 가장 가까운 파츠를 따르게 하고,
    어떤 파츠와도 이어지지 않은 조각만 연결 요소마다 새 묶음으로 만든다.

    Returns:
        (면별 묶음 번호, 전체 묶음 수)
    """

    result = list(assignment)
    faces_by_vertex: dict[int, list[int]] = {}
    for face, vertices in enumerate(face_vertices):
        for vertex in vertices:
            faces_by_vertex.setdefault(vertex, []).append(face)

    def spread(queue: deque) -> None:
        while queue:
            face = queue.popleft()
            for vertex in face_vertices[face]:
                for neighbor in faces_by_vertex[vertex]:
                    if result[neighbor] < 0:
                        result[neighbor] = result[face]
                        queue.append(neighbor)

    spread(deque(face for face, group in enumerate(result) if group >= 0))
    count = group_count
    for face, group in enumerate(result):
        if group < 0:
            result[face] = count
            spread(deque([face]))
            count += 1
    return result, count


def _area(mask: Mapping[int, int]) -> int:
    return sum(bits.bit_count() for bits in mask.values())


def _dilate(mask: Mapping[int, int], radius: int) -> dict[int, int]:
    """가로·세로로 ``radius`` 칸 넓힌 마스크."""

    if radius <= 0:
        return dict(mask)
    widened = {}
    for row, bits in mask.items():
        spread = bits
        for step in range(1, radius + 1):
            spread |= (bits << step) | (bits >> step)
        widened[row] = spread
    result: dict[int, int] = {}
    for row, bits in widened.items():
        for offset in range(-radius, radius + 1):
            result[row + offset] = result.get(row + offset, 0) | bits
    return result


def _overlap(first: Mapping[int, int], first_shift: int, second: Mapping[int, int], second_shift: int) -> int:
    """두 마스크를 각자 가로로 옮겼을 때 겹치는 칸 수."""

    relative = first_shift - second_shift
    count = 0
    for row, bits in first.items():
        other = second.get(row)
        if not other:
            continue
        if relative >= 0:
            count += ((bits << relative) & other).bit_count()
        else:
            count += (bits & (other << -relative)).bit_count()
    return count


def _column_span(mask: Mapping[int, int]) -> tuple[int, int]:
    combined = 0
    for bits in mask.values():
        combined |= bits
    if not combined:
        return 0, 0
    low = (combined & -combined).bit_length() - 1
    return low, combined.bit_length() - 1


def _centroid_column(mask: Mapping[int, int]) -> float:
    total = 0
    weighted = 0.0
    for bits in mask.values():
        column = 0
        while bits:
            if bits & 1:
                total += 1
                weighted += column
            bits >>= 1
            column += 1
    return weighted / total if total else 0.0


def _placement_tree(areas: Sequence[float], adjacency: Mapping[int, set[int]]) -> list[tuple[int, dict[int, list[int]]]]:
    """연결 요소마다 (뿌리, 부모 → 자식 목록) 너비 우선 트리. 큰 파츠가 먼저다.

    가장 큰 파츠(몸통)를 뿌리로 두면 팔·다리·머리가 자식, 손·발이 손자가 된다.
    배치는 자식 하위 트리(팔+손)를 한 덩어리로 옮기므로 손이 팔에서 떨어지지 않는다.
    """

    trees = []
    seen: set[int] = set()
    for root in sorted(range(len(areas)), key=lambda index: (-areas[index], index)):
        if root in seen:
            continue
        seen.add(root)
        children: dict[int, list[int]] = {}
        queue = deque([root])
        while queue:
            index = queue.popleft()
            for neighbor in sorted(adjacency.get(index, ()), key=lambda item: (-areas[item], item)):
                if neighbor not in seen:
                    seen.add(neighbor)
                    children.setdefault(index, []).append(neighbor)
                    queue.append(neighbor)
        trees.append((root, children))
    return trees


def _plan_view(
    groups: Sequence[Sequence[Triangle]],
    adjacency: Mapping[int, set[int]],
    areas: Sequence[float],
    view: str,
    grid: int,
    gap_ratio: float,
    attach_tolerance: float,
    touch_tolerance: float,
) -> tuple[tuple[float, float], ...]:
    shift_axis, fixed_axis, vertical = _shift_axes(view)
    points = [point for triangles in groups for triangle in triangles for point in triangle]
    shift_values = [_dot(point, shift_axis) for point in points]
    fixed_values = [_dot(point, fixed_axis) for point in points]
    span = max(max(shift_values) - min(shift_values), max(fixed_values) - min(fixed_values), 1.0e-6)
    cell = span / grid
    # 음수 이동도 비트 연산으로 다루도록 원점을 왼쪽으로 충분히 민다.
    margin = (_MAX_SHIFT_SPANS + 1) * grid
    origin = (min(shift_values) - margin * cell, min(fixed_values))
    masks = [_rasterize(triangles, shift_axis, fixed_axis, origin, cell) for triangles in groups]
    pixel_areas = [_area(mask) for mask in masks]
    gap = max(1, int(math.ceil(gap_ratio * grid)))
    dilated = [_dilate(mask, gap) for mask in masks]
    centroids = [_centroid_column(mask) for mask in masks]
    limit = _MAX_SHIFT_SPANS * grid

    def pair_fits(first: int, first_shift: int, second: int, second_shift: int) -> bool:
        if not pixel_areas[first] or not pixel_areas[second]:
            return True
        if first_shift == second_shift:
            # 원래 자세의 상대 위치 그대로다. 이미 떨어져 있던 파츠(정면의 두 다리)는
            # 빈틈이 좁아도 옮기지 않고, 실제로 겹쳐 가리는 경우만 옮긴다.
            ratio = attach_tolerance if second in adjacency.get(first, ()) else touch_tolerance
            allowed = ratio * min(pixel_areas[first], pixel_areas[second])
            return _overlap(masks[first], first_shift, masks[second], second_shift) <= allowed
        # 옮긴 파츠는 AI가 조각을 붙여 그리지 않도록 빈틈을 두고 떨어뜨린다.
        return not _overlap(dilated[first], first_shift, masks[second], second_shift)

    def attach(unit: Mapping[int, int], placed: Mapping[int, int], direction: int) -> int:
        """``unit``(파츠 → 상대 이동)을 ``placed``와 겹치지 않게 놓을 가장 짧은 이동."""

        for distance in range(0, limit + 1):
            for sign in ((direction, -direction) if distance else (1,)):
                offset = sign * distance
                if all(
                    pair_fits(member, shift + offset, other, other_shift)
                    for member, shift in unit.items()
                    for other, other_shift in placed.items()
                ):
                    return offset
        return 0

    def unit_centroid(unit: Mapping[int, int]) -> float:
        total = sum(pixel_areas[member] for member in unit)
        if not total:
            return 0.0
        return sum((centroids[member] + shift) * pixel_areas[member] for member, shift in unit.items()) / total

    def layout(node: int, children: Mapping[int, list[int]]) -> dict[int, int]:
        """``node``를 0에 두고 자식 하위 트리를 차례로 붙인 상대 배치."""

        unit = {node: 0}
        for child in children.get(node, ()):
            sub = layout(child, children)
            direction = 1 if unit_centroid(sub) >= centroids[node] else -1
            offset = attach(sub, unit, direction)
            unit.update({member: shift + offset for member, shift in sub.items()})
        return unit

    shifts: dict[int, int] = {}
    for root, children in _placement_tree(areas, adjacency):
        unit = layout(root, children)
        direction = 1 if not shifts or unit_centroid(unit) >= unit_centroid(shifts) else -1
        offset = attach(unit, shifts, direction) if shifts else 0
        shifts.update({member: shift + offset for member, shift in unit.items()})

    if not any(shifts.values()):
        return tuple((0.0, 0.0) for _ in groups)
    # 벌린 배치 전체의 가운데를 원래 모델 가운데에 맞춰 카메라 프레이밍을 유지한다.
    present = [index for index in range(len(groups)) if pixel_areas[index]]
    low = min(_column_span(masks[index])[0] + shifts[index] for index in present)
    high = max(_column_span(masks[index])[1] + shifts[index] for index in present)
    original_low = min(_column_span(masks[index])[0] for index in present)
    original_high = max(_column_span(masks[index])[1] for index in present)
    recenter = ((original_low + original_high) - (low + high)) * 0.5
    result = []
    for index in range(len(groups)):
        amount = (shifts.get(index, 0) + recenter) * cell
        result.append((0.0, amount) if vertical else (amount, 0.0))
    return tuple(result)


def plan_explode(
    groups: Sequence[Sequence[Triangle]],
    adjacency: Iterable[tuple[int, int]],
    views: Sequence[str],
    *,
    grid: int = EXPLODE_GRID,
    gap_ratio: float = EXPLODE_GAP_RATIO,
    attach_tolerance: float = EXPLODE_ATTACH_TOLERANCE,
    touch_tolerance: float = EXPLODE_TOUCH_TOLERANCE,
) -> dict[str, tuple[tuple[float, float], ...]]:
    """시점마다 파츠별 화면 평면 이동량을 정한다.

    Args:
        groups: 파츠마다의 월드 좌표 삼각형 목록. 파츠에 속하지 않은 면도 하나의
            묶음으로 넣는다(빈 목록은 이동하지 않는다).
        adjacency: 정점을 공유해 이어진 파츠 쌍(인덱스)
        views: 이동량을 정할 시점 이름

    Returns:
        시점 → 파츠 순서대로의 ``(right, up)`` 이동량(월드 단위). 겹치는 파츠가
        없는 시점은 모두 0이다.
    """

    if grid < 16:
        raise ValueError("분해도 격자는 16칸 이상이어야 합니다.")
    links: dict[int, set[int]] = {}
    for first, second in adjacency:
        if first == second:
            continue
        links.setdefault(first, set()).add(second)
        links.setdefault(second, set()).add(first)
    areas = [sum(triangle_area(triangle) for triangle in triangles) for triangles in groups]
    if not any(areas):
        raise ValueError("분해도를 계산할 삼각형이 없습니다.")
    plan = {}
    for view in views:
        name = str(view).upper()
        if name not in VIEW_SPECS:
            raise ValueError(f"지원하지 않는 시점입니다: {view}")
        plan[name] = _plan_view(groups, links, areas, name, grid, gap_ratio, attach_tolerance, touch_tolerance)
    return plan


def explode_piece_counts(shifts_by_view: Mapping[str, Sequence[Sequence[float]]]) -> dict[str, int]:
    """시점마다 가이드에 보이는 떨어진 조각 수(같은 만큼 옮긴 파츠 묶음 수).

    같은 양만큼 옮긴 파츠는 원래 이어진 채로 함께 움직인 것이므로 한 조각으로 센다.
    생성 요청에 칸별 조각 수를 알려 AI가 조각을 빼먹거나 다시 붙이지 않게 한다.
    """

    counts = {}
    for view, shifts in shifts_by_view.items():
        distinct = {(round(float(shift[0]), 6), round(float(shift[1]), 6)) for shift in shifts}
        counts[str(view).upper()] = max(1, len(distinct))
    return counts


def exploded_half_extent(
    groups: Sequence[Sequence[Triangle]],
    plan: Mapping[str, Sequence[tuple[float, float]]],
    center: Vec3,
) -> float:
    """벌린 배치가 모든 시점에서 화면 중심으로부터 뻗는 최대 거리(가로·세로 중 큰 값)."""

    reach = 0.0
    for view, shifts in plan.items():
        spec = VIEW_SPECS[view]
        for triangles, (shift_right, shift_up) in zip(groups, shifts):
            for triangle in triangles:
                for point in triangle:
                    offset = (point[0] - center[0], point[1] - center[1], point[2] - center[2])
                    reach = max(
                        reach,
                        abs(_dot(offset, spec.right) + shift_right),
                        abs(_dot(offset, spec.up) + shift_up),
                    )
    return reach


__all__ = (
    "EXPLODE_ATTACH_TOLERANCE",
    "EXPLODE_GAP_RATIO",
    "EXPLODE_GRID",
    "EXPLODE_TOUCH_TOLERANCE",
    "assign_unregistered_faces",
    "explode_piece_counts",
    "exploded_half_extent",
    "plan_explode",
    "triangle_area",
)
