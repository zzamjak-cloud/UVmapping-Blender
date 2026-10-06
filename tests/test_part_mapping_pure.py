"""Blender 없이 실행하는 파츠별 매핑(패스 구성·프롬프트·가림 제거·합성) 테스트."""

from __future__ import annotations

import base64
from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.mapping_parts import CUSTOM_KIND, part_key, plan_part_passes
from uvmapping.texture_bake import (
    BakeTriangle,
    boundary_blend_weight,
    build_raster_source,
    composite_part_layer,
    rasterize_atlas,
)
from uvmapping.texture_pipeline import (
    CONTACT_SHEET_ROLE,
    MAX_PART_PASSES,
    InlineImage,
    build_part_refine_request,
    compile_part_refine_prompt,
    part_refine_layout_name,
)


GREY = (0.25, 0.25, 0.25)


def _solid_source(color: tuple[float, float, float, float]):
    return build_raster_source(
        width=4,
        height=4,
        pixels=list(color * 16),
        subject_bbox=(0, 0, 4, 4),
        background=(1.0, 1.0, 1.0, 1.0),
        background_threshold=0.05,
        fallback_color=color,
    )


def _sources():
    return {
        "FRONT": _solid_source((0.8, 0.1, 0.05, 1.0)),
        "RIGHT": _solid_source((0.05, 0.8, 0.1, 1.0)),
        "BACK": _solid_source((0.05, 0.1, 0.8, 1.0)),
    }


# 정면에서 볼 때 앞 삼각형(팔)이 뒤 삼각형(몸통)을 완전히 가린다.
_ARM = BakeTriangle(
    positions=((-0.4, -0.35, -0.4), (0.4, -0.35, -0.4), (0.0, -0.35, 0.4)),
    uvs=((0.05, 0.05), (0.45, 0.05), (0.25, 0.45)),
    normal=(0.0, -1.0, 0.0),
)
_TORSO = BakeTriangle(
    positions=((-0.4, 0.25, -0.4), (0.4, 0.25, -0.4), (0.0, 0.25, 0.4)),
    uvs=((0.55, 0.05), (0.95, 0.05), (0.75, 0.45)),
    normal=(0.0, -1.0, 0.0),
)
_TORSO_PIXEL = (8 * 32 + 24) * 4


def test_part_keys_group_presets_and_custom_names() -> None:
    assert part_key("HEAD", "아무 이름") == "HEAD"
    assert part_key(CUSTOM_KIND, " Tail ") == part_key(CUSTOM_KIND, "tail")
    assert part_key(CUSTOM_KIND, "꼬리") != part_key(CUSTOM_KIND, "날개")


def test_plan_orders_presets_then_custom_and_merges_objects() -> None:
    plan = plan_part_passes(
        [
            ("Body", "CUSTOM", "Tail", "a"),
            ("Body", "ARM_L", "왼팔", "b"),
            ("Head", "HEAD", "머리", "c"),
            ("Hat", "HEAD", "모자 포함 머리", "d"),
            ("Body", "TORSO", "몸통", "e"),
            ("Body", "CUSTOM", "Belt", "f"),
            ("Body", "CUSTOM", "tail", "g"),
        ]
    )
    assert [item.key for item in plan] == ["HEAD", "TORSO", "ARM_L", "CUSTOM:belt", "CUSTOM:tail"]
    head = plan[0]
    assert head.label == "머리" and head.prompt_label == "head"
    assert head.members == (("Hat", "d"), ("Head", "c"))
    tail = plan[-1]
    assert tail.members == (("Body", "a"), ("Body", "g"))
    assert tail.prompt_label == "Tail"


def test_part_layout_follows_main_composition() -> None:
    assert part_refine_layout_name(("FRONT", "RIGHT", "BACK")) == "THREE"
    assert part_refine_layout_name(("FRONT", "RIGHT", "BACK", "LEFT", "TOP", "BOTTOM")) == "SIX"
    assert MAX_PART_PASSES >= 10, "기본 인체 단위 10종은 한 번에 처리할 수 있어야 한다"


def test_part_refine_prompt_names_part_and_grey_contract() -> None:
    prompt = compile_part_refine_prompt(None, "빨간 재킷", part_label="left arm", layout="SIX")
    assert "대상 부위: left arm" in prompt
    assert "회색 영역" in prompt and "가려" in prompt
    assert "숨겨진 다른 부위를 새로 그려 넣지 않습니다" in prompt
    assert "3열과 같은 높이의 2행" in prompt
    assert "빨간 재킷" in prompt


def test_part_refine_request_keeps_single_call_contract() -> None:
    sheet = InlineImage("image/png", base64.b64encode(b"x").decode("ascii"), CONTACT_SHEET_ROLE, "g.png")
    request = build_part_refine_request(
        sheet, (), None, "지시", part_label="head", layout_name="THREE", image_size="1K"
    )
    assert request.layout_name == "THREE" and request.aspect_ratio == "21:9"
    assert request.views == ("FRONT", "RIGHT", "BACK")
    assert request.provider_call_count == 1
    try:
        build_part_refine_request(sheet, (), None, "지시", part_label="head", layout_name="QUAD_SIDES")
    except ValueError:
        pass
    else:
        raise AssertionError("그룹 레이아웃은 파츠별 매핑에 쓰지 않는다")


def test_full_pass_leaves_occluded_torso_grey_without_fallback() -> None:
    # 전신 패스: 팔이 몸통을 가리므로 가이드용 베이크에서는 몸통이 회색으로 남아야 한다.
    rgba, metrics = rasterize_atlas(
        (_ARM, _TORSO), _sources(), 32, 0, (0.0, 0.0, 0.0), 1.0,
        unpainted_color=GREY, occlusion_fallback=False,
    )
    assert metrics["occluded_fallback_pixels"] == 0
    assert metrics["unpainted_pixels"] > 0
    grey = rgba[_TORSO_PIXEL : _TORSO_PIXEL + 3]
    assert max(grey) - min(grey) <= 2, "가려진 몸통은 회색이어야 한다"
    # 기본값은 지금처럼 가린 표본으로 메운다.
    _rgba, default_metrics = rasterize_atlas((_ARM, _TORSO), _sources(), 32, 0, (0.0, 0.0, 0.0), 1.0)
    assert default_metrics["occluded_fallback_pixels"] > 0


def test_part_pass_without_arm_sees_torso_directly() -> None:
    # 파츠 패스: 팔을 숨기면(삼각형에서 빼면) 몸통이 정면에서 직접 보인다.
    coverage = bytearray(32 * 32)
    rgba, metrics = rasterize_atlas(
        (_TORSO,), _sources(), 32, 0, (0.0, 0.0, 0.0), 1.0, coverage=coverage
    )
    assert metrics["occluded_samples"] == 0
    assert metrics["occluded_fallback_pixels"] == 0
    assert rgba[_TORSO_PIXEL] > rgba[_TORSO_PIXEL + 1]
    assert coverage[_TORSO_PIXEL // 4] == 255
    assert coverage[(8 * 32 + 8)] == 0, "팔 UV 영역은 이 파츠가 칠하지 않는다"


def test_coverage_interpolates_corner_blend_weights() -> None:
    triangle = BakeTriangle(
        positions=_TORSO.positions,
        uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        normal=(0.0, -1.0, 0.0),
        blend_weights=(0.0, 1.0, 1.0),
    )
    coverage = bytearray(16 * 16)
    rasterize_atlas((triangle,), _sources(), 16, 0, (0.0, 0.0, 0.0), 1.0, coverage=coverage)
    near_zero_corner = coverage[0 * 16 + 0]
    far_corner = coverage[0 * 16 + 14]
    assert near_zero_corner < 40 < far_corner, (near_zero_corner, far_corner)
    try:
        rasterize_atlas((triangle,), _sources(), 16, 0, (0.0, 0.0, 0.0), 1.0, coverage=bytearray(3))
    except ValueError:
        pass
    else:
        raise AssertionError("coverage 크기를 검증해야 한다")


def test_boundary_blend_weight_is_smooth() -> None:
    assert boundary_blend_weight(0.0, 1.0) == 0.0
    assert boundary_blend_weight(1.0, 1.0) == 1.0
    assert boundary_blend_weight(5.0, 1.0) == 1.0
    assert boundary_blend_weight(0.5, 1.0) == 0.5
    assert boundary_blend_weight(0.0, 0.0) == 1.0, "혼합 폭 0이면 경계에서 바로 파츠 결과"
    assert boundary_blend_weight(0.25, 1.0) < 0.25, "smoothstep은 경계 근처에서 완만하다"


def _check_composite() -> None:
    base = bytearray([100, 100, 100, 255] * 3)
    layer = bytes([200, 0, 50, 255] * 3)
    changed = composite_part_layer(base, layer, bytearray([0, 255, 128]))
    assert changed == 2
    assert list(base[0:4]) == [100, 100, 100, 255]
    assert list(base[4:8]) == [200, 0, 50, 255]
    assert list(base[8:11]) == [150, 50, 75]


def test_composite_part_layer_respects_weights() -> None:
    _check_composite()
    # numpy가 없는 환경의 순수 루프도 같은 결과를 내야 한다.
    saved = sys.modules.get("numpy", ...)
    sys.modules["numpy"] = None  # type: ignore[assignment]
    try:
        _check_composite()
    finally:
        if saved is ...:
            sys.modules.pop("numpy", None)
        else:
            sys.modules["numpy"] = saved
    try:
        composite_part_layer(bytearray(8), bytes(4), [0, 0])
    except ValueError:
        pass
    else:
        raise AssertionError("버퍼 크기를 검증해야 한다")


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 파츠별 매핑 테스트 {len(tests)}/{len(tests)} 통과")
