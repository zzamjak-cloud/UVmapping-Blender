"""Blender 없이 실행하는 파츠 분해도(배치 계산·면 배정·이동 투영·프롬프트) 테스트."""

from __future__ import annotations

import base64
from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.explode_layout import (
    assign_unregistered_faces,
    explode_piece_counts,
    exploded_half_extent,
    plan_explode,
)
from uvmapping.texture_bake import VIEW_NAMES, BakeTriangle, build_raster_source, rasterize_atlas
from uvmapping.texture_pipeline import (
    CONTACT_SHEET_ROLE,
    InlineImage,
    build_turnaround_request,
    compile_sequential_view_prompt,
    compile_turnaround_prompt,
)


def _box(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> list:
    """축 정렬 상자의 삼각형 12개."""

    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3))
    triangles = []
    for a, b, c, d in faces:
        triangles.append((corners[a], corners[b], corners[c]))
        triangles.append((corners[a], corners[c], corners[d]))
    return triangles


# 몸통 양옆에 팔이 조금 떨어져 붙어 있고, 오른팔 아래에 손이 이어진 A포즈 축소 모형.
_TORSO = _box(-0.2, 0.2, -0.1, 0.1, 0.5, 1.0)
_ARM_R = _box(0.22, 0.3, -0.1, 0.1, 0.6, 0.95)
_ARM_L = _box(-0.3, -0.22, -0.1, 0.1, 0.6, 0.95)
_HAND_R = _box(0.22, 0.3, -0.1, 0.1, 0.5, 0.6)
_GROUPS = [_TORSO, _ARM_R, _ARM_L, _HAND_R]
_ADJACENCY = [(0, 1), (0, 2), (1, 3)]
_VIEWS = ("FRONT", "RIGHT", "BACK", "LEFT", "TOP", "BOTTOM")


def _plan():
    return plan_explode(_GROUPS, _ADJACENCY, _VIEWS)


def test_front_keeps_already_separated_parts_in_place() -> None:
    plan = _plan()
    # 정면에서는 팔과 몸통이 이미 떨어져 있어 아무것도 옮기지 않는다.
    assert all(shift == (0.0, 0.0) for shift in plan["FRONT"]), plan["FRONT"]
    assert all(shift == (0.0, 0.0) for shift in plan["BACK"]), plan["BACK"]


def test_side_view_pulls_arms_out_of_torso_without_changing_height() -> None:
    plan = _plan()
    for view in ("RIGHT", "LEFT"):
        torso, arm_r, arm_l, hand_r = plan[view]
        # 측면은 가로로만 옮겨 벨트·소매 끝 높이가 시점 사이에서 그대로 이어진다.
        assert all(shift[1] == 0.0 for shift in plan[view]), plan[view]
        # 몸통 깊이(y 0.2)만큼 이상 벌어져야 서로 가리지 않는다.
        assert abs(arm_r[0] - torso[0]) >= 0.2, (view, plan[view])
        assert abs(arm_l[0] - torso[0]) >= 0.2, (view, plan[view])
        assert abs(arm_r[0] - arm_l[0]) >= 0.2, (view, plan[view])
        # 두 팔은 몸통 양쪽으로 나뉜다.
        assert (arm_r[0] - torso[0]) * (arm_l[0] - torso[0]) < 0.0, (view, plan[view])
        # 손은 팔과 함께 움직여 떨어져 나가지 않는다.
        assert hand_r == arm_r, (view, plan[view])
    # 측면은 몸통, 오른팔+손, 왼팔 세 조각이고 정면은 한 덩어리다.
    counts = explode_piece_counts(plan)
    assert counts["RIGHT"] == 3 and counts["LEFT"] == 3, counts
    assert counts["FRONT"] == 1, counts


def test_top_and_bottom_shift_along_screen_vertical() -> None:
    plan = _plan()
    for view in ("TOP", "BOTTOM"):
        assert all(shift[0] == 0.0 for shift in plan[view]), plan[view]
        # 위에서 보면 손이 팔에 가려지므로(겹침이 연결부 허용치를 넘음) 손은 따로 옮겨진다.
        assert plan[view][3] != plan[view][1], plan[view]


def test_exploded_extent_covers_shifted_parts() -> None:
    plan = _plan()
    reach = exploded_half_extent(_GROUPS, plan, (0.0, 0.0, 0.75))
    # 원래 모델의 화면 반폭(0.3)보다 넓어야 카메라가 벌린 팔까지 담는다.
    assert reach > 0.3, reach
    try:
        plan_explode(_GROUPS, _ADJACENCY, ("SIDEWAYS",))
    except ValueError:
        pass
    else:
        raise AssertionError("모르는 시점은 거부해야 한다")


def test_unregistered_faces_follow_connected_part() -> None:
    # 면 1(발바닥)은 등록되지 않았지만 면 0(발)과 정점 2를 공유한다. 면 2는 어디와도 이어지지 않는다.
    faces = [(0, 1, 2), (2, 3, 4), (5, 6, 7)]
    assignment, count = assign_unregistered_faces(faces, [0, -1, -1], 1)
    assert assignment == [0, 0, 1], assignment
    assert count == 2


def _split_source(left, right):
    """왼쪽 절반과 오른쪽 절반 색이 다른 8x4 생성 그림."""

    pixels = []
    for _row in range(4):
        for column in range(8):
            pixels.extend(left if column < 4 else right)
    return build_raster_source(
        width=8,
        height=4,
        pixels=pixels,
        subject_bbox=(0, 0, 8, 4),
        background=(1.0, 1.0, 1.0, 1.0),
        background_threshold=0.05,
        fallback_color=left,
    )


_RED = (0.8, 0.1, 0.05, 1.0)
_GREEN = (0.05, 0.8, 0.1, 1.0)
_FRONT_ARM = BakeTriangle(
    positions=((-0.4, -0.35, -0.4), (0.4, -0.35, -0.4), (0.0, -0.35, 0.4)),
    uvs=((0.05, 0.05), (0.45, 0.05), (0.25, 0.45)),
    normal=(0.0, -1.0, 0.0),
)
_BACK_TORSO = BakeTriangle(
    positions=((-0.4, 0.25, -0.4), (0.4, 0.25, -0.4), (0.0, 0.25, 0.4)),
    uvs=((0.55, 0.05), (0.95, 0.05), (0.75, 0.45)),
    normal=(0.0, -1.0, 0.0),
)
_TORSO_PIXEL = (8 * 32 + 24) * 4


def _sources():
    return {
        "FRONT": _split_source(_RED, _GREEN),
        "RIGHT": _split_source(_RED, _RED),
        "BACK": _split_source(_RED, _RED),
    }


def test_shifted_part_is_projected_where_the_exploded_guide_drew_it() -> None:
    # 원래 자세: 정면에서 앞 삼각형(팔)이 뒤 삼각형(몸통)을 완전히 가린다.
    _rgba, hidden = rasterize_atlas((_FRONT_ARM, _BACK_TORSO), _sources(), 32, 0, (0.0, 0.0, 0.0), 3.0)
    assert hidden["occluded_samples"] > 0
    # 분해도: 몸통을 정면에서 오른쪽으로 0.9 옮겨 그렸다면, 같은 만큼 옮겨 투영해 가림이 없다.
    shifts = tuple((0.9, 0.0) if view == "FRONT" else (0.0, 0.0) for view in VIEW_NAMES)
    torso = BakeTriangle(
        positions=_BACK_TORSO.positions,
        uvs=_BACK_TORSO.uvs,
        normal=_BACK_TORSO.normal,
        view_shifts=shifts,
    )
    rgba, metrics = rasterize_atlas((_FRONT_ARM, torso), _sources(), 32, 0, (0.0, 0.0, 0.0), 3.0)
    assert metrics["occluded_samples"] == 0, metrics
    assert metrics["occluded_fallback_pixels"] == 0
    # 옮겨 그린 몸통은 생성 그림의 오른쪽 절반(초록)에서 색을 읽는다.
    assert rgba[_TORSO_PIXEL + 1] > rgba[_TORSO_PIXEL], tuple(rgba[_TORSO_PIXEL : _TORSO_PIXEL + 3])


def test_prompts_explain_exploded_guide_only_when_used() -> None:
    plain = compile_turnaround_prompt(None, "우주복", layout="SIX")
    pieces = {"FRONT": 1, "RIGHT": 4, "BACK": 1, "LEFT": 4, "TOP": 1, "BOTTOM": 1}
    exploded = compile_turnaround_prompt(None, "우주복", layout="SIX", exploded_pieces=pieces)
    assert "분해도" not in plain
    assert "분해도(exploded view)" in exploded
    assert "조각 사이의 빈 공간은 배경으로 남깁니다" in exploded
    assert "옆구리, 팔 안쪽, 다리 안쪽" in exploded
    assert "칸별 회색 조각 수:" in exploded and "4개" in exploded, exploded
    sequential = compile_sequential_view_prompt(None, "우주복", view="RIGHT", exploded_pieces={"RIGHT": 4})
    assert "분해도(exploded view)" in sequential
    sheet = InlineImage("image/png", base64.b64encode(b"x").decode("ascii"), CONTACT_SHEET_ROLE, "g.png")
    request = build_turnaround_request(sheet, (), None, "우주복", layout_name="SIX", image_size="1K", exploded_pieces=pieces)
    assert "분해도(exploded view)" in request.prompt
    assert request.provider_call_count == 1, "분해도도 호출 1회 계약을 지킨다"


def test_foreground_bbox_keeps_distant_large_pieces_but_not_labels() -> None:
    from uvmapping.texture_bake import detect_foreground_bbox

    width, height = 200, 100
    pixels = [1.0] * (width * height * 4)

    def paint(x0: int, x1: int, y0: int, y1: int) -> None:
        for y in range(y0, y1):
            for x in range(x0, x1):
                offset = (y * width + x) * 4
                pixels[offset : offset + 3] = [0.3, 0.4, 0.8]

    # 양끝에 떨어진 두 팔(같은 크기)과, 멀리 놓인 작은 글자 조각
    paint(10, 30, 20, 80)
    paint(170, 190, 20, 80)
    paint(95, 98, 2, 5)
    left, bottom, right, top = detect_foreground_bbox(pixels, width, height)
    assert left <= 10 and right >= 190, (left, right)
    assert bottom >= 15, "작은 글자 조각은 피사체 범위에 넣지 않는다"


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 파츠 분해도 테스트 {len(tests)}/{len(tests)} 통과")
