"""Blender 없이 실행하는 매핑 파츠 인체 단위 프리셋·이름 매칭 테스트."""

from __future__ import annotations

from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.mapping_parts import (
    BODY_PART_PRESETS,
    CUSTOM_KIND,
    default_part_name,
    duplicate_kinds,
    kind_enum_items,
    kind_label,
    match_body_part,
    next_unregistered_kind,
    prompt_label,
)


def test_presets_cover_basic_body_units() -> None:
    kinds = [preset.kind for preset in BODY_PART_PRESETS]
    assert kinds == [
        "HEAD", "TORSO", "ARM_L", "ARM_R", "HAND_L", "HAND_R",
        "LEG_L", "LEG_R", "FOOT_L", "FOOT_R",
    ]
    assert len(set(preset.label for preset in BODY_PART_PRESETS)) == len(kinds)


def test_enum_items_end_with_custom_input() -> None:
    items = kind_enum_items()
    assert [item[0] for item in items[:-1]] == [preset.kind for preset in BODY_PART_PRESETS]
    assert items[-1][0] == CUSTOM_KIND
    assert kind_label(CUSTOM_KIND) == "사용자 지정"
    assert kind_label("ARM_L") == "왼팔"


def test_korean_names_match_presets() -> None:
    cases = {
        "머리": "HEAD",
        "얼굴": "HEAD",
        "몸통": "TORSO",
        "상체": "TORSO",
        "왼팔": "ARM_L",
        "오른팔": "ARM_R",
        "좌측 팔": "ARM_L",
        "우측 팔": "ARM_R",
        "왼손": "HAND_L",
        "오른 손목": "HAND_R",
        "왼다리": "LEG_L",
        "오른쪽 허벅지": "LEG_R",
        "왼발": "FOOT_L",
        "오른 신발": "FOOT_R",
    }
    for name, kind in cases.items():
        assert match_body_part(name) == kind, (name, match_body_part(name))


def test_english_names_match_presets() -> None:
    cases = {
        "Head": "HEAD",
        "face_mesh": "HEAD",
        "Body": "TORSO",
        "Torso01": "TORSO",
        "LeftArm": "ARM_L",
        "arm.R": "ARM_R",
        "upper_arm_l": "ARM_L",
        "ARM_L": "ARM_L",
        "RightForearm": "ARM_R",
        "hand.L": "HAND_L",
        "Glove_R": "HAND_R",
        "leg_left": "LEG_L",
        "RThigh": "LEG_R",
        "foot.R": "FOOT_R",
        "LeftShoe": "FOOT_L",
    }
    for name, kind in cases.items():
        assert match_body_part(name) == kind, (name, match_body_part(name))


def test_english_keywords_need_whole_tokens() -> None:
    # 부분 문자열 오탐을 막는다: warmer(arm), surface(face), ship(hip), handle(hand)
    assert match_body_part("Leg Warmer_L") == "LEG_L"
    for name in ("Surface", "Interface", "Ship", "Whip", "Nobody", "Handle_L", "Shine_R", "Bootstrap"):
        assert match_body_part(name) == CUSTOM_KIND, (name, match_body_part(name))
    # 복수형과 접두 복합어는 인정한다.
    assert match_body_part("Hands_L") == "HAND_L"
    assert match_body_part("lowerleg.R") == "LEG_R"
    assert match_body_part("leftarm") == "ARM_L"


def test_excluded_words_drop_only_their_token() -> None:
    assert match_body_part("Chest Armor") == "TORSO"
    assert match_body_part("Left Arm Armor") == "ARM_L"
    assert match_body_part("Head and Hair") == "HEAD"
    assert match_body_part("갑옷 몸통") == "TORSO"
    assert match_body_part("머리카락") == CUSTOM_KIND


def test_korean_side_words_ignore_single_syllables() -> None:
    # "우"·"좌" 한 음절은 좌우로 보지 않는다.
    assert match_body_part("우산 든 팔") == CUSTOM_KIND
    assert match_body_part("우측 우산 든 팔") == "ARM_R"


def test_hand_and_foot_win_over_arm_and_leg() -> None:
    # 손목·발목은 팔·다리 단어보다 손·발 단어가 먼저 걸려야 한다.
    assert match_body_part("왼 손목") == "HAND_L"
    assert match_body_part("왼발목") == "FOOT_L"


def test_unmatched_or_ambiguous_names_stay_custom() -> None:
    for name in (
        "", "   ", "꼬리", "Tail", "날개", "모자",
        # 인체 단어를 포함하지만 인체 단위가 아닌 이름
        "머리카락", "Hair_Front", "Armor", "Backpack",
        # 좌우가 필요한 부위인데 좌우를 정할 수 없는 이름
        "팔", "arm", "양팔", "LeftRightArm",
    ):
        assert match_body_part(name) == CUSTOM_KIND, (name, match_body_part(name))


def test_next_unregistered_kind_walks_presets_in_order() -> None:
    assert next_unregistered_kind([]) == "HEAD"
    assert next_unregistered_kind(["HEAD"]) == "TORSO"
    assert next_unregistered_kind(["HEAD", "TORSO", CUSTOM_KIND]) == "ARM_L"
    assert next_unregistered_kind([preset.kind for preset in BODY_PART_PRESETS]) == CUSTOM_KIND


def test_default_part_name_avoids_collisions() -> None:
    assert default_part_name("HEAD", [], 1) == "머리"
    assert default_part_name("HEAD", ["머리"], 2) == "머리.001"
    assert default_part_name("HEAD", ["머리", "머리.001"], 3) == "머리.002"
    assert default_part_name(CUSTOM_KIND, ["머리"], 2) == "파츠 2"


def test_prompt_label_and_duplicates() -> None:
    assert prompt_label("ARM_R", "아무 이름") == "right arm"
    assert prompt_label(CUSTOM_KIND, " 꼬리 ") == "꼬리"
    assert prompt_label(CUSTOM_KIND, "") == "part"
    assert duplicate_kinds(["HEAD", "TORSO", "HEAD", CUSTOM_KIND, CUSTOM_KIND, "HEAD"]) == ("HEAD",)


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 매핑 파츠 테스트 {len(tests)}/{len(tests)} 통과")
