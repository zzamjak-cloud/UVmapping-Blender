"""품질 프리셋 표와 호출 수 계산을 bpy 없이 다루는 순수 계층.

`properties.py`는 bpy 없이는 import되지 않으므로, 프리셋 표·CUSTOM 판정·텍스처 크기
선택지처럼 순수하게 결정되는 부분만 여기로 분리해 순수 테스트로 고정한다.
"""

from __future__ import annotations

from typing import Any, Mapping

from .texture_pipeline import resolve_composition


# 프리셋이 덮어쓰는 개별 프로퍼티. 생성 방식·패딩·참조 전달·블렌딩류는 건드리지 않는다.
QUALITY_PRESET_FIELDS = (
    "turnaround_layout",
    "turnaround_image_size",
    "texture_resolution",
    "auto_regenerate_attempts",
    "verify_after_bake",
    "texture_image_quality",
)
CUSTOM_QUALITY_PRESET = "CUSTOM"
DEFAULT_QUALITY_PRESET = "STANDARD"
# "프리셋이 정한 등급을 그대로 쓴다"는 뜻이라 어떤 프리셋과도 어긋나지 않는다.
AUTO_IMAGE_QUALITY = "AUTO"
# 프리셋 표가 없는 CUSTOM에서 AUTO를 고르면 쓸 등급.
CUSTOM_AUTO_IMAGE_QUALITY = "HIGH"
IMAGE_QUALITY_ITEMS = (
    (AUTO_IMAGE_QUALITY, "자동", "품질 프리셋이 정한 등급을 씁니다"),
    ("MEDIUM", "보통", "medium 등급 · 가장 저렴"),
    ("HIGH", "높음", "high 등급"),
    (
        "XHIGH",
        "매우 높음",
        "xhigh 등급 · GPT Image 2.5 계열만 받으며 가장 비쌈. 받지 못하는 모델은 high로 낮춥니다",
    ),
)
QUALITY_PRESETS: Mapping[str, Mapping[str, Any]] = {
    "FAST": {
        "turnaround_layout": "THREE",
        "turnaround_image_size": "AUTO",
        "texture_resolution": "1024",
        "auto_regenerate_attempts": 0,
        "verify_after_bake": False,
        "texture_image_quality": "MEDIUM",
    },
    "STANDARD": {
        "turnaround_layout": "SIX",
        "turnaround_image_size": "AUTO",
        "texture_resolution": "1024",
        "auto_regenerate_attempts": 1,
        "verify_after_bake": True,
        "texture_image_quality": "HIGH",
    },
    "HIGH": {
        "turnaround_layout": "QUAD",
        "turnaround_image_size": "AUTO",
        "texture_resolution": "2048",
        "auto_regenerate_attempts": 1,
        "verify_after_bake": True,
        "texture_image_quality": "XHIGH",
    },
}
QUALITY_PRESET_ITEMS = (
    ("FAST", "빠름", "3면도 1회 호출 · 1K 텍스처 · 다시 그리기와 검증 끔 · 가장 저렴"),
    ("STANDARD", "표준", "6면도 1회 호출 · 1K 텍스처 · 어긋나면 1회 다시 그리기 · 적용 후 검증"),
    ("HIGH", "고품질", "6면을 4회로 나눠 호출 · 2K 텍스처 · 비용 약 4배"),
    (CUSTOM_QUALITY_PRESET, "사용자 지정", "고급 설정에서 직접 고른 값을 씁니다"),
)
# 베이크 상한(texture_bake.MAX_ATLAS_RESOLUTION)을 넘는 선택지는 고르는 순간 예외가 된다.
TEXTURE_RESOLUTION_OPTIONS = (
    ("256", "256 px", "작은 소품용 256 px 텍스처"),
    ("512", "512 px", "512 px 텍스처"),
    ("1024", "1024 px (1K)", "1K 텍스처"),
    ("2048", "2048 px (2K)", "2K 텍스처"),
    ("4096", "4096 px (4K)", "4K 텍스처 · 지원하는 최대 크기"),
)


def normalize_quality_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """프리셋 비교에 쓸 형식으로 값을 맞춘다. Blender 프로퍼티는 문자열·정수·불리언으로 온다."""

    return {
        "turnaround_layout": str(values.get("turnaround_layout", "")).upper(),
        "turnaround_image_size": str(values.get("turnaround_image_size", "")).upper(),
        "texture_resolution": str(values.get("texture_resolution", "")),
        "auto_regenerate_attempts": int(values.get("auto_regenerate_attempts", 0)),
        "verify_after_bake": bool(values.get("verify_after_bake", False)),
        "texture_image_quality": str(
            values.get("texture_image_quality", AUTO_IMAGE_QUALITY)
        ).upper(),
    }


def _values_match(preset: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    """값 묶음이 프리셋 표와 같은지. 품질 등급 AUTO는 어떤 프리셋과도 어긋나지 않는다."""

    expected = normalize_quality_values(preset)
    actual = normalize_quality_values(values)
    if actual["texture_image_quality"] == AUTO_IMAGE_QUALITY:
        actual["texture_image_quality"] = expected["texture_image_quality"]
    return expected == actual


def quality_preset_values(preset: str) -> dict[str, Any] | None:
    """프리셋이 개별 프로퍼티에 써 넣을 값. CUSTOM과 모르는 이름은 ``None``."""

    table = QUALITY_PRESETS.get(str(preset).upper())
    return dict(table) if table is not None else None


def matching_quality_preset(values: Mapping[str, Any]) -> str:
    """현재 값과 정확히 일치하는 프리셋 이름. 없으면 CUSTOM."""

    for name, preset in QUALITY_PRESETS.items():
        if _values_match(preset, values):
            return name
    return CUSTOM_QUALITY_PRESET


def quality_preset_after_change(current: str, values: Mapping[str, Any]) -> str:
    """개별 값을 직접 바꾼 뒤 표시할 프리셋.

    현재 프리셋의 표와 그대로 일치하면 유지하고, 어긋나면 CUSTOM으로 내린다. 다른
    프리셋 표와 우연히 일치해도 사용자가 고른 적 없는 이름으로 바꾸지는 않는다.
    """

    name = str(current).upper()
    table = QUALITY_PRESETS.get(name)
    if table is None:
        return CUSTOM_QUALITY_PRESET
    if _values_match(table, values):
        return name
    return CUSTOM_QUALITY_PRESET


def resolved_image_quality(preset: str, quality: str) -> str:
    """실제로 보낼 소문자 품질 등급.

    AUTO는 "프리셋이 정한 등급을 쓴다"는 뜻이라 표에서 가져온다. 표가 없는 CUSTOM은
    이미지 크기를 1K로 고정당하는 모델에서 가장 무난한 HIGH를 쓴다.
    """

    value = str(quality or "").strip().upper()
    if value and value != AUTO_IMAGE_QUALITY:
        return value.lower()
    table = QUALITY_PRESETS.get(str(preset).upper())
    if table is None:
        return CUSTOM_AUTO_IMAGE_QUALITY.lower()
    return str(table["texture_image_quality"]).lower()


def turnaround_call_count(layout: str, generation_mode: str) -> int:
    """구성과 생성 방식으로 정해지는 OpenRouter 호출 수(재생성 제외)."""

    composition = resolve_composition(layout)
    if str(generation_mode).upper() == "SEQUENTIAL":
        return len(composition.views)
    return len(composition.groups)


__all__ = (
    "AUTO_IMAGE_QUALITY",
    "CUSTOM_AUTO_IMAGE_QUALITY",
    "CUSTOM_QUALITY_PRESET",
    "DEFAULT_QUALITY_PRESET",
    "IMAGE_QUALITY_ITEMS",
    "QUALITY_PRESETS",
    "QUALITY_PRESET_FIELDS",
    "QUALITY_PRESET_ITEMS",
    "TEXTURE_RESOLUTION_OPTIONS",
    "matching_quality_preset",
    "normalize_quality_values",
    "quality_preset_after_change",
    "quality_preset_values",
    "resolved_image_quality",
    "turnaround_call_count",
)
