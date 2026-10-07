"""매핑 파츠의 기본 인체 단위 표와 이름 매칭 규칙.

파츠는 사용자가 면(쿼드) 단위로 등록하는 매핑용 메시 그룹이다. 머리·몸통·팔·
다리처럼 자주 쓰는 인체 단위는 프리셋으로 고를 수 있게 하고, 사용자가 직접
입력한 이름도 아래 매칭 단어로 같은 인체 단위로 인식한다. 이 모듈은 ``bpy``
없이 동작해 순수 테스트로 검증한다.

좌우는 **캐릭터 기준**이다. 정면에서 볼 때 화면 오른쪽에 있는 팔이 캐릭터의
왼팔이다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


# 프리셋이 아닌, 사용자가 직접 이름을 붙인 파츠 종류
CUSTOM_KIND = "CUSTOM"


@dataclass(frozen=True)
class BodyPartPreset:
    """기본 인체 단위 하나.

    Attributes:
        kind: 저장·비교용 식별자
        label: 패널에 보이는 한국어 이름(기본 파츠 이름으로도 쓴다)
        prompt_label: 패널 설명에 함께 보이는 영어 이름
        part: 좌우를 뺀 부위 키(매칭 단어 표의 키)
        side: ``"L"``·``"R"`` 또는 좌우가 없는 부위면 ``""``
    """

    kind: str
    label: str
    prompt_label: str
    part: str
    side: str = ""


# 패널 드롭다운과 "다음 미등록 파츠" 순서. A포즈 캐릭터를 위에서 아래로 나눈 순서다.
BODY_PART_PRESETS: tuple[BodyPartPreset, ...] = (
    BodyPartPreset("HEAD", "머리", "head", "HEAD"),
    BodyPartPreset("TORSO", "몸통", "torso", "TORSO"),
    BodyPartPreset("ARM_L", "왼팔", "left arm", "ARM", "L"),
    BodyPartPreset("ARM_R", "오른팔", "right arm", "ARM", "R"),
    BodyPartPreset("HAND_L", "왼손", "left hand", "HAND", "L"),
    BodyPartPreset("HAND_R", "오른손", "right hand", "HAND", "R"),
    BodyPartPreset("LEG_L", "왼다리", "left leg", "LEG", "L"),
    BodyPartPreset("LEG_R", "오른다리", "right leg", "LEG", "R"),
    BodyPartPreset("FOOT_L", "왼발", "left foot", "FOOT", "L"),
    BodyPartPreset("FOOT_R", "오른발", "right foot", "FOOT", "R"),
)

_PRESETS_BY_KIND = {preset.kind: preset for preset in BODY_PART_PRESETS}
_PRESETS_BY_PART_SIDE = {(preset.part, preset.side): preset for preset in BODY_PART_PRESETS}

# 부위별 매칭 단어. 한국어는 이름 안 부분 문자열로, 영어는 토큰 단위로 찾는다.
# 검사 순서가 중요하다: 손·발을 팔·다리보다 먼저 봐야 "손목"·"발목"이 손·발이 된다.
_PART_KEYWORDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("HAND", ("손", "장갑"), ("hand", "glove", "palm", "finger", "fist", "wrist")),
    ("FOOT", ("발", "신발", "부츠"), ("foot", "feet", "shoe", "boot", "toe", "ankle")),
    ("HEAD", ("머리", "얼굴", "두상", "안면"), ("head", "face", "skull")),
    ("ARM", ("팔", "소매", "어깨"), ("arm", "sleeve", "shoulder", "elbow")),
    ("LEG", ("다리", "허벅지", "종아리", "무릎"), ("leg", "thigh", "calf", "shin", "knee")),
    (
        "TORSO",
        ("몸통", "몸", "상체", "가슴", "동체", "허리", "골반", "바디"),
        ("torso", "body", "chest", "trunk", "spine", "pelvis", "hip", "waist", "belly"),
    ),
)

# 영어 키워드는 토큰이 정확히 같거나, 복수형이거나, 아래 접두사가 붙은 복합어일 때만
# 인정한다. 부분 문자열로 찾으면 "warmer"→arm, "surface"→face처럼 오탐이 많다.
_ENGLISH_PREFIXES = ("", "upper", "lower", "fore", "front", "back", "inner", "outer", "left", "right", "l", "r")
_ENGLISH_SUFFIXES = ("", "s", "es")

# 매칭 단어를 포함하지만 인체 단위가 아닌 말. 그 단어(토큰)만 지우고 나머지로 매칭한다.
# 예: "Chest Armor"는 armor를 지우고 chest로 몸통, "머리카락"은 통째로 지워져 사용자 지정.
_EXCLUDED_KOREAN = ("머리카락", "머리털", "헤어", "갑옷", "배낭")
_EXCLUDED_ENGLISH = ("hair", "armor", "armour", "armature", "armband", "headphone", "backpack", "handle")

# 한 음절("우", "좌")은 우산·좌석 같은 말에 섞여 오판하므로 확장형만 쓴다.
_LEFT_KOREAN = ("왼", "좌측")
_RIGHT_KOREAN = ("오른", "우측")
_LEFT_TOKENS = ("l", "lt", "left", "lft")
_RIGHT_TOKENS = ("r", "rt", "right", "rgt")

# 영어 토큰 분리: 영문자·숫자 덩어리, camelCase 경계
_TOKEN_PATTERN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_HANGUL_PATTERN = re.compile(r"[가-힣]+")


def preset_for_kind(kind: str) -> BodyPartPreset | None:
    """종류 식별자에 해당하는 프리셋. 사용자 지정 종류면 ``None``."""

    return _PRESETS_BY_KIND.get(kind)


def kind_label(kind: str) -> str:
    """패널에 보일 종류 이름."""

    preset = preset_for_kind(kind)
    return preset.label if preset else "사용자 지정"


def kind_enum_items() -> list[tuple[str, str, str]]:
    """Blender EnumProperty 항목: 프리셋 10종 + 직접 입력."""

    items = [
        (preset.kind, preset.label, f"{preset.label} ({preset.prompt_label})")
        for preset in BODY_PART_PRESETS
    ]
    items.append((CUSTOM_KIND, "직접 입력", "프리셋에 없는 파츠 이름을 직접 입력합니다"))
    return items


def _tokens(name: str) -> list[str]:
    """영어 이름을 소문자 토큰으로 나눈다. ``LeftArm``·``arm.L``·``upper_arm_r`` 모두 처리."""

    return [token.lower() for token in _TOKEN_PATTERN.findall(name)]


def _side(korean: str, tokens: list[str]) -> str:
    """이름에서 좌우를 찾는다. 양쪽이 모두 보이거나 없으면 빈 문자열."""

    left = any(word in korean for word in _LEFT_KOREAN) or any(
        token in _LEFT_TOKENS or token.startswith("left") for token in tokens
    )
    right = any(word in korean for word in _RIGHT_KOREAN) or any(
        token in _RIGHT_TOKENS or token.startswith("right") for token in tokens
    )
    if left == right:
        return ""
    return "L" if left else "R"


def _english_token_matches(token: str, word: str) -> bool:
    """토큰이 키워드와 같거나 복수형·접두 복합어(forearm, upperleg, leftarm 등)인지."""

    for prefix in _ENGLISH_PREFIXES:
        if not token.startswith(prefix):
            continue
        rest = token[len(prefix):]
        if any(rest == word + suffix for suffix in _ENGLISH_SUFFIXES):
            return True
    return False


def _part(korean: str, tokens: list[str]) -> str:
    """좌우를 뺀 부위 키. 매칭되지 않으면 빈 문자열."""

    for word in _EXCLUDED_KOREAN:
        korean = korean.replace(word, " ")
    tokens = [
        token
        for token in tokens
        if not any(_english_token_matches(token, word) for word in _EXCLUDED_ENGLISH)
    ]
    for part, korean_words, english_words in _PART_KEYWORDS:
        if any(word in korean for word in korean_words):
            return part
        if any(_english_token_matches(token, word) for token in tokens for word in english_words):
            return part
    return ""


def match_body_part(name: str) -> str:
    """이름을 기본 인체 단위 종류로 매칭한다.

    Args:
        name: 사용자가 입력한 파츠 이름(한국어·영어·혼합)

    Returns:
        프리셋 종류 식별자. 부위를 찾지 못했거나, 좌우가 필요한 부위인데 좌우를
        정할 수 없으면 ``CUSTOM_KIND``.
    """

    text = (name or "").strip()
    if not text:
        return CUSTOM_KIND
    korean = " ".join(_HANGUL_PATTERN.findall(text))
    tokens = _tokens(text)
    part = _part(korean, tokens)
    if not part:
        return CUSTOM_KIND
    preset = _PRESETS_BY_PART_SIDE.get((part, ""))
    if preset is not None:
        return preset.kind
    side = _side(korean, tokens)
    preset = _PRESETS_BY_PART_SIDE.get((part, side))
    return preset.kind if preset else CUSTOM_KIND


def next_unregistered_kind(registered_kinds) -> str:
    """아직 등록되지 않은 첫 프리셋. 모두 등록됐으면 직접 입력.

    파츠를 연달아 등록할 때 사용자가 매번 종류를 고르지 않아도 되게 한다.
    """

    used = set(registered_kinds)
    for preset in BODY_PART_PRESETS:
        if preset.kind not in used:
            return preset.kind
    return CUSTOM_KIND


def default_part_name(kind: str, existing_names, fallback_index: int) -> str:
    """새 파츠의 기본 이름. 프리셋이면 그 이름, 겹치면 ``.001``처럼 번호를 붙인다."""

    preset = preset_for_kind(kind)
    base = preset.label if preset else f"파츠 {fallback_index}"
    names = set(existing_names)
    if base not in names:
        return base
    number = 1
    while f"{base}.{number:03d}" in names:
        number += 1
    return f"{base}.{number:03d}"


def duplicate_kinds(kinds) -> tuple[str, ...]:
    """같은 프리셋 종류가 두 번 이상 등록된 목록(사용자 지정은 제외)."""

    seen: set[str] = set()
    duplicates: list[str] = []
    for kind in kinds:
        if kind == CUSTOM_KIND:
            continue
        if kind in seen and kind not in duplicates:
            duplicates.append(kind)
        seen.add(kind)
    return tuple(duplicates)


__all__ = (
    "BODY_PART_PRESETS",
    "BodyPartPreset",
    "CUSTOM_KIND",
    "default_part_name",
    "duplicate_kinds",
    "kind_enum_items",
    "kind_label",
    "match_body_part",
    "next_unregistered_kind",
    "preset_for_kind",
)
