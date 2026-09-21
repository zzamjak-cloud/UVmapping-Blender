"""Blender 없이 실행하는 품질 프리셋·텍스처 크기 선택지·호출 수 테스트.

`properties.py`는 bpy 없이는 import되지 않으므로, 프리셋 표와 CUSTOM 판정처럼 순수하게
결정되는 부분만 여기서 고정한다.
"""

from __future__ import annotations

from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_bake import MAX_ATLAS_RESOLUTION
from uvmapping.texture_pipeline import IMAGE_QUALITY_OPTIONS
from uvmapping.texture_presets import (
    AUTO_IMAGE_QUALITY,
    CUSTOM_AUTO_IMAGE_QUALITY,
    CUSTOM_QUALITY_PRESET,
    IMAGE_QUALITY_ITEMS,
    DEFAULT_QUALITY_PRESET,
    QUALITY_PRESETS,
    QUALITY_PRESET_FIELDS,
    QUALITY_PRESET_ITEMS,
    TEXTURE_RESOLUTION_OPTIONS,
    matching_quality_preset,
    quality_preset_after_change,
    quality_preset_values,
    resolved_image_quality,
    turnaround_call_count,
)


def test_preset_table_matches_plan() -> None:
    assert QUALITY_PRESETS["FAST"] == {
        "turnaround_layout": "THREE",
        "turnaround_image_size": "AUTO",
        "texture_resolution": "1024",
        "auto_regenerate_attempts": 0,
        "verify_after_bake": False,
        "texture_image_quality": "MEDIUM",
    }
    assert QUALITY_PRESETS["STANDARD"] == {
        "turnaround_layout": "SIX",
        "turnaround_image_size": "AUTO",
        "texture_resolution": "1024",
        "auto_regenerate_attempts": 1,
        "verify_after_bake": True,
        "texture_image_quality": "HIGH",
    }
    assert QUALITY_PRESETS["HIGH"] == {
        "turnaround_layout": "QUAD",
        "turnaround_image_size": "AUTO",
        "texture_resolution": "2048",
        "auto_regenerate_attempts": 1,
        "verify_after_bake": True,
        "texture_image_quality": "XHIGH",
    }
    assert DEFAULT_QUALITY_PRESET == "STANDARD"
    assert all(set(values) == set(QUALITY_PRESET_FIELDS) for values in QUALITY_PRESETS.values())


def test_preset_enum_items_cover_table_and_custom() -> None:
    identifiers = tuple(item[0] for item in QUALITY_PRESET_ITEMS)
    assert identifiers == ("FAST", "STANDARD", "HIGH", CUSTOM_QUALITY_PRESET)
    assert set(QUALITY_PRESETS) == set(identifiers) - {CUSTOM_QUALITY_PRESET}
    assert all(len(item) == 3 and all(item) for item in QUALITY_PRESET_ITEMS)


def test_quality_preset_values_copies_and_ignores_custom() -> None:
    values = quality_preset_values("HIGH")
    assert values is not None
    values["texture_resolution"] = "256"
    assert QUALITY_PRESETS["HIGH"]["texture_resolution"] == "2048", "표가 수정되면 안 됩니다."
    assert quality_preset_values(CUSTOM_QUALITY_PRESET) is None
    assert quality_preset_values("없는프리셋") is None


def test_matching_preset_identifies_each_table() -> None:
    for name, values in QUALITY_PRESETS.items():
        assert matching_quality_preset(values) == name
    assert matching_quality_preset({**QUALITY_PRESETS["STANDARD"], "padding_pixels": 8}) == "STANDARD"


def test_matching_preset_falls_back_to_custom() -> None:
    changed = {**QUALITY_PRESETS["STANDARD"], "texture_resolution": "4096"}
    assert matching_quality_preset(changed) == CUSTOM_QUALITY_PRESET
    assert matching_quality_preset({}) == CUSTOM_QUALITY_PRESET


def test_preset_after_change_keeps_or_drops_to_custom() -> None:
    # 값이 표와 그대로면 프리셋 표시를 유지한다.
    assert quality_preset_after_change("STANDARD", QUALITY_PRESETS["STANDARD"]) == "STANDARD"
    # 한 값이라도 어긋나면 CUSTOM.
    changed = {**QUALITY_PRESETS["STANDARD"], "verify_after_bake": False}
    assert quality_preset_after_change("STANDARD", changed) == CUSTOM_QUALITY_PRESET
    # 다른 프리셋 표와 우연히 일치해도 고른 적 없는 이름으로 바꾸지 않는다.
    assert quality_preset_after_change("STANDARD", QUALITY_PRESETS["HIGH"]) == CUSTOM_QUALITY_PRESET
    # CUSTOM에서는 어떤 값이어도 CUSTOM에 머문다.
    assert (
        quality_preset_after_change(CUSTOM_QUALITY_PRESET, QUALITY_PRESETS["FAST"])
        == CUSTOM_QUALITY_PRESET
    )


def test_preset_values_normalize_blender_types() -> None:
    # Blender 프로퍼티는 불리언 0/1과 정수 문자열로도 올 수 있다.
    from_blender = {
        "turnaround_layout": "six",
        "turnaround_image_size": "auto",
        "texture_resolution": "1024",
        "auto_regenerate_attempts": 1,
        "verify_after_bake": 1,
        "texture_image_quality": "high",
    }
    assert matching_quality_preset(from_blender) == "STANDARD"


def test_image_quality_items_map_to_provider_grades() -> None:
    identifiers = tuple(item[0] for item in IMAGE_QUALITY_ITEMS)
    assert identifiers == (AUTO_IMAGE_QUALITY, "MEDIUM", "HIGH", "XHIGH")
    assert all(len(item) == 3 and all(item) for item in IMAGE_QUALITY_ITEMS)
    # AUTO를 뺀 등급은 파이프라인이 받는 소문자 등급과 1:1로 대응해야 한다.
    for identifier in identifiers:
        if identifier == AUTO_IMAGE_QUALITY:
            continue
        assert identifier.lower() in IMAGE_QUALITY_OPTIONS, identifier
    # 프리셋이 써 넣는 등급도 모두 유효해야 한다.
    for values in QUALITY_PRESETS.values():
        assert values["texture_image_quality"] in identifiers


def test_auto_image_quality_never_forces_custom() -> None:
    # 기본값 AUTO는 "프리셋이 정한 등급을 쓴다"는 뜻이므로 표시를 흔들지 않는다.
    for name, values in QUALITY_PRESETS.items():
        with_auto = {**values, "texture_image_quality": AUTO_IMAGE_QUALITY}
        assert matching_quality_preset(with_auto) == name
        assert quality_preset_after_change(name, with_auto) == name
    # 프리셋과 다른 등급을 직접 고르면 CUSTOM으로 내려간다.
    changed = {**QUALITY_PRESETS["STANDARD"], "texture_image_quality": "MEDIUM"}
    assert quality_preset_after_change("STANDARD", changed) == CUSTOM_QUALITY_PRESET


def test_resolved_quality_follows_preset_then_explicit_choice() -> None:
    # AUTO는 현재 프리셋의 매핑값을 그대로 쓴다.
    for name, values in QUALITY_PRESETS.items():
        expected = str(values["texture_image_quality"]).lower()
        assert resolved_image_quality(name, AUTO_IMAGE_QUALITY) == expected, name
        assert resolved_image_quality(name, "") == expected, name
    # 표가 없는 CUSTOM에서는 정해진 기본 등급을 쓴다.
    assert resolved_image_quality(CUSTOM_QUALITY_PRESET, AUTO_IMAGE_QUALITY) == (
        CUSTOM_AUTO_IMAGE_QUALITY.lower()
    )
    assert resolved_image_quality("없는프리셋", AUTO_IMAGE_QUALITY) == "high"
    # 직접 고른 등급은 프리셋과 무관하게 그대로 간다.
    for preset in (*QUALITY_PRESETS, CUSTOM_QUALITY_PRESET):
        assert resolved_image_quality(preset, "MEDIUM") == "medium", preset
        assert resolved_image_quality(preset, "XHIGH") == "xhigh", preset
    # 등급은 항상 Provider가 받는 소문자 형식이다.
    assert all(
        resolved_image_quality(preset, AUTO_IMAGE_QUALITY) in IMAGE_QUALITY_OPTIONS
        for preset in (*QUALITY_PRESETS, CUSTOM_QUALITY_PRESET)
    )


def test_texture_resolution_options_stay_within_bake_limit() -> None:
    identifiers = tuple(item[0] for item in TEXTURE_RESOLUTION_OPTIONS)
    assert "8192" not in identifiers, "베이크 상한을 넘는 선택지가 남아 있습니다."
    assert max(int(value) for value in identifiers) == MAX_ATLAS_RESOLUTION
    assert "1024" in identifiers
    assert all(len(item) == 3 and all(item) for item in TEXTURE_RESOLUTION_OPTIONS)
    # 프리셋이 쓰는 값은 모두 선택지 안에 있어야 한다.
    for values in QUALITY_PRESETS.values():
        assert values["texture_resolution"] in identifiers


def test_call_count_follows_composition_and_mode() -> None:
    assert turnaround_call_count("THREE", "SINGLE") == 1
    assert turnaround_call_count("SIX", "SINGLE") == 1
    assert turnaround_call_count("QUAD", "SINGLE") == 4
    assert turnaround_call_count("THREE", "SEQUENTIAL") == 3
    assert turnaround_call_count("SIX", "SEQUENTIAL") == 6
    assert turnaround_call_count("QUAD", "SEQUENTIAL") == 6
    # 프리셋별 호출 수는 계획서 표와 같아야 한다.
    expected = {"FAST": 1, "STANDARD": 1, "HIGH": 4}
    for name, count in expected.items():
        assert turnaround_call_count(QUALITY_PRESETS[name]["turnaround_layout"], "SINGLE") == count


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 품질 프리셋 테스트 {len(tests)}/{len(tests)} 통과")
