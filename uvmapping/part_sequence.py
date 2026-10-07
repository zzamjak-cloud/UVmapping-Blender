"""부위별 순차 생성의 그룹 구성 규칙.

등록 파츠를 머리·몸통, 팔·다리(손·발 포함) 두 그룹으로 묶고, 그룹마다 그 부위만 렌더한
다면도를 한 번씩 생성한다. 사용자 지정 파츠와 등록하지 않은 면은 그룹을 정하지 않고(-1)
두어, 정점으로 이어진 그룹을 따르게 한다.

머리·몸통을 먼저 생성하고 팔·다리는 그 그림을 참조로 생성한다. 허리띠처럼 가장 길게
이어지는 무늬가 몸통에 있고, 어깨·골반 연결부의 기준도 몸통이기 때문이다.

이 모듈은 ``bpy`` 없이 동작해 순수 테스트로 검증한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .mapping_parts import preset_for_kind


@dataclass(frozen=True)
class PartGroup:
    """부위별 순차 생성의 그룹 하나(생성 호출 1회).

    Attributes:
        key: 저장·비교용 식별자
        label: 패널·상태 줄에 보일 이름
        prompt_label: 생성 프롬프트에 넣는 부위 설명
        parts: 이 그룹에 드는 인체 단위 부위 키(:class:`BodyPartPreset.part`)
    """

    key: str
    label: str
    prompt_label: str
    parts: tuple[str, ...]


# 생성 순서. 머리·몸통이 첫 라운드, 팔·다리는 그 결과를 참조해 두 번째 라운드에 요청한다.
# 머리를 따로 생성하면 둥근 형상만으로는 앞뒤를 알아보지 못해 품질이 떨어졌다(실측). 몸통과 한 장에
# 그리면 몸통의 앞뒤가 방향 단서가 되고 목 연결부도 한 그림 안에서 이어진다.
PART_GROUPS: tuple[PartGroup, ...] = (
    PartGroup(
        "BODY",
        "머리·몸통",
        "head and torso (face or helmet visor and chest on the side seen in the FRONT cell)",
        ("HEAD", "TORSO"),
    ),
    PartGroup("LIMBS", "팔·다리", "both arms with both hands and both legs with both feet", ("ARM", "HAND", "LEG", "FOOT")),
)
BASE_GROUP_KEY = "BODY"
_GROUP_INDEX_BY_PART = {part: index for index, group in enumerate(PART_GROUPS) for part in group.parts}


def group_index_for_kind(kind: str) -> int:
    """파츠 종류가 드는 그룹 번호. 사용자 지정 파츠는 -1(이어진 그룹을 따른다)."""

    preset = preset_for_kind(kind)
    if preset is None:
        return -1
    return _GROUP_INDEX_BY_PART.get(preset.part, -1)


def face_group_indices(
    face_vertices: Sequence[Sequence[int]], part_of_face: Sequence[int], part_kinds: Sequence[str]
) -> list[int]:
    """면마다 드는 그룹 번호(:data:`PART_GROUPS` 순서).

    Args:
        face_vertices: 면마다의 정점 번호
        part_of_face: 면마다의 파츠 번호(등록 순서). 파츠에 없는 면은 파츠 수 이상이다
            (:func:`explode_layout.assign_unregistered_faces` 결과).
        part_kinds: 파츠 번호 순서대로의 종류

    Returns:
        그룹 번호 목록. 사용자 지정 파츠와 파츠 없는 면은 정점으로 이어진 그룹을 따르고,
        어떤 그룹과도 이어지지 않은 조각은 첫 그룹(머리·몸통)이다.
    """

    from .explode_layout import assign_unregistered_faces

    seeds = [
        group_index_for_kind(part_kinds[part]) if 0 <= part < len(part_kinds) else -1 for part in part_of_face
    ]
    groups, _count = assign_unregistered_faces(face_vertices, seeds, len(PART_GROUPS))
    base = next(index for index, group in enumerate(PART_GROUPS) if group.key == BASE_GROUP_KEY)
    return [group if group < len(PART_GROUPS) else base for group in groups]


def used_groups(face_groups: Sequence[int]) -> tuple[PartGroup, ...]:
    """면이 하나라도 있는 그룹(생성 순서)."""

    present = set(face_groups)
    return tuple(group for index, group in enumerate(PART_GROUPS) if index in present)


__all__ = (
    "BASE_GROUP_KEY",
    "PART_GROUPS",
    "PartGroup",
    "face_group_indices",
    "group_index_for_kind",
    "used_groups",
)
