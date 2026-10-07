"""Blender 없이 실행하는 부위별 순차 생성(그룹 구성·프롬프트·호출 수) 테스트."""

from __future__ import annotations

import base64
from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.mapping_parts import CUSTOM_KIND
from uvmapping.part_sequence import (
    BASE_GROUP_KEY,
    PART_GROUPS,
    face_group_indices,
    group_index_for_kind,
    used_groups,
)
from uvmapping.texture_pipeline import CONTACT_SHEET_ROLE, USER_REFERENCE_ROLE, InlineImage, build_turnaround_request
from uvmapping.texture_presets import turnaround_call_count


def _index(key: str) -> int:
    return next(index for index, group in enumerate(PART_GROUPS) if group.key == key)


def test_groups_are_head_torso_then_limbs() -> None:
    assert [group.key for group in PART_GROUPS] == ["BODY", "LIMBS"]
    assert PART_GROUPS[0].key == BASE_GROUP_KEY, "머리·몸통을 먼저 생성해야 한다"
    for kind in ("HEAD", "TORSO"):
        assert group_index_for_kind(kind) == _index("BODY"), kind
    for kind in ("ARM_L", "ARM_R", "HAND_L", "HAND_R", "LEG_L", "LEG_R", "FOOT_L", "FOOT_R"):
        assert group_index_for_kind(kind) == _index("LIMBS"), kind
    assert group_index_for_kind(CUSTOM_KIND) == -1


def test_unregistered_and_custom_faces_follow_connected_group() -> None:
    # 면0 몸통, 면1 왼발, 면2 발바닥(파츠 없음, 발과 정점 공유), 면3 꼬리(사용자 지정, 몸통과 공유), 면4 고립 조각
    faces = [(0, 1, 2), (10, 11, 12), (12, 13, 14), (2, 3, 4), (20, 21, 22)]
    part_of_face = [0, 1, 3, 2, 4]
    kinds = ["TORSO", "FOOT_L", CUSTOM_KIND]
    groups = face_group_indices(faces, part_of_face, kinds)
    assert groups == [_index("BODY"), _index("LIMBS"), _index("LIMBS"), _index("BODY"), _index("BODY")], groups
    assert [group.key for group in used_groups(groups)] == ["BODY", "LIMBS"]


def test_part_focus_prompt_introduces_torso_reference() -> None:
    sheet = InlineImage("image/png", base64.b64encode(b"x").decode("ascii"), CONTACT_SHEET_ROLE, "g.png")
    torso = InlineImage("image/png", base64.b64encode(b"y").decode("ascii"), USER_REFERENCE_ROLE, "t.png")
    first = build_turnaround_request(
        sheet, (), None, "우주복", layout_name="SIX", image_size="1K", part_focus="torso"
    )
    assert "같은 캐릭터의 'torso'만 있습니다" in first.prompt
    assert "먼저 완성한 다면도" not in first.prompt
    later = build_turnaround_request(
        sheet,
        (torso,),
        None,
        "우주복",
        layout_name="SIX",
        image_size="1K",
        part_focus="both arms with both hands",
        base_reference=True,
        exploded_pieces={"RIGHT": 2, "LEFT": 2},
    )
    assert "'both arms with both hands'만 있습니다" in later.prompt
    assert "두 번째 입력 이미지는 같은 캐릭터의 머리와 몸통을 먼저 완성한 다면도입니다" in later.prompt
    assert "분해도(exploded view)" in later.prompt
    assert later.provider_call_count == 1


def test_call_count_for_part_sequence_is_at_most_two() -> None:
    assert turnaround_call_count("SIX", "PART_SEQUENCE") == 2
    assert turnaround_call_count("SIX", "SINGLE") == 1


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 부위별 순차 생성 테스트 {len(tests)}/{len(tests)} 통과")
