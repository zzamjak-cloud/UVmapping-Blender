"""OpenRouter 분석·이미지 생성 요청을 만드는 Blender 비의존 순수 함수.

두 Provider를 각자의 API로 호출하던 구조를 OpenRouter 한 곳으로 합쳤다.
참조 분석은 OpenAI 호환 ``/chat/completions``, 3면도 생성은 ``/images``를 쓰며
어떤 모델을 고르든 요청·응답 형식은 동일하다.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Mapping, NamedTuple, Sequence

from .texture_pipeline import ASPECT_RATIO_OPTIONS, IMAGE_SIZE_OPTIONS


BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_ENDPOINT = "chat/completions"
IMAGES_ENDPOINT = "images"

DEFAULT_ANALYSIS_MODEL = "google/gemini-3.7-flash"
DEFAULT_IMAGE_MODEL = "google/gemini-3-pro-image"
DEFAULT_ASPECT_RATIO = "21:9"
DEFAULT_RESOLUTION = "2K"
# OpenRouter /images가 받는 해상도 등급. 레이아웃 종횡비는 호출자가 명시한다.
# 해상도·종횡비 화이트리스트는 파이프라인 레이아웃 계약 한 곳에서만 정의한다.
RESOLUTION_OPTIONS = IMAGE_SIZE_OPTIONS

# 애드온 사용량을 OpenRouter 대시보드에서 구분하기 위한 선택적 출처 헤더.
APP_TITLE = "UV Mapping Blender"
APP_URL = "https://github.com/zzamjak-cloud/UVmapping-Blender"

# OpenRouter가 image-to-image 참조로 허용하는 최대 장수.
MAX_INPUT_REFERENCES = 16


class ImageModelCapability(NamedTuple):
    """이미지 모델 한 종류가 받는 파라미터 집합."""

    aspect_ratios: tuple[str, ...]
    resolutions: tuple[str, ...]
    qualities: tuple[str, ...]
    max_input_references: int


# 실측 출처: OpenRouter ``/api/v1/images/models`` 응답(2026-09-21).
_GEMINI_ASPECT_RATIOS = (
    "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
)
_GPT_ASPECT_RATIOS = (
    "1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9", "auto",
)
_GPT_QUALITIES = ("auto", "low", "medium", "high")
_GPT_25_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")

# 모델마다 받는 파라미터가 다르다. Gemini 계열은 resolution을, GPT 계열은 quality를 쓴다.
IMAGE_MODEL_CAPABILITIES: dict[str, ImageModelCapability] = {
    "google/gemini-3-pro-image": ImageModelCapability(
        _GEMINI_ASPECT_RATIOS, ("1K", "2K", "4K"), (), 14
    ),
    "google/gemini-3.1-flash-image": ImageModelCapability(
        _GEMINI_ASPECT_RATIOS, ("512", "1K", "2K", "4K"), (), 14
    ),
    # flash-lite는 1K 한 등급만 받는다.
    "google/gemini-3.1-flash-lite-image": ImageModelCapability(
        _GEMINI_ASPECT_RATIOS, ("1K",), (), 14
    ),
    "openai/gpt-5.4-image-2": ImageModelCapability(
        _GPT_ASPECT_RATIOS, (), _GPT_QUALITIES, 16
    ),
    "openai/gpt-image-2": ImageModelCapability(
        _GPT_ASPECT_RATIOS, (), _GPT_QUALITIES, 16
    ),
    "openai/gpt-image-2.5-sunburst": ImageModelCapability(
        _GPT_ASPECT_RATIOS, (), _GPT_25_QUALITIES, 16
    ),
    "openai/gpt-image-2.5-flare": ImageModelCapability(
        _GPT_ASPECT_RATIOS, (), _GPT_25_QUALITIES, 16
    ),
}

# 해상도 등급의 높낮이 순서. 모델이 받지 않는 등급은 이 순서에서 아래로 낮춘다.
_RESOLUTION_LADDER = ("4K", "2K", "1K", "512")
# quality 등급의 높낮이 순서. 모델이 받지 않는 등급은 이 순서에서 아래로 낮춘다.
_QUALITY_LADDER = ("max", "xhigh", "high", "medium", "low")
# 등급 이름 전체. 여기에 없으면 오타로 보고 거부한다.
_KNOWN_QUALITIES = _QUALITY_LADDER + ("auto",)

# 생성 이미지 크기를 quality 등급으로 옮길 때의 선호 순서. 앞의 값부터 모델이 받는 것을 쓴다.
_IMAGE_SIZE_QUALITY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "1K": ("medium", "low", "auto"),
    "2K": ("high", "medium", "auto"),
    "4K": ("xhigh", "high", "medium", "auto"),
}

_MODEL_PATTERN = re.compile(r"^[0-9A-Za-z._-]+/[0-9A-Za-z._:-]+$")
_IMAGE_MIME_PATTERN = re.compile(r"^image/[0-9A-Za-z!#$&'*+.^_`|~-]+$")
_SUPPORTED_OUTPUT_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")


def _require_non_empty_text(value: Any, label: str) -> str:
    """필수 문자열을 검사하고 원래 값을 보존한다."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}이(가) 비어 있습니다.")
    return value


def validate_model_slug(model: Any) -> str:
    """OpenRouter 모델 식별자가 ``vendor/model`` 형식인지 검사한다."""

    value = _require_non_empty_text(model, "OpenRouter 모델").strip()
    if _MODEL_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "OpenRouter 모델은 google/gemini-3-pro-image처럼 "
            "'제공자/모델' 형식이어야 합니다."
        )
    return value


def model_capabilities(model: Any) -> ImageModelCapability | None:
    """등재된 이미지 모델의 능력치를 돌려준다. 모르는 슬러그면 ``None``."""

    if not isinstance(model, str):
        return None
    return IMAGE_MODEL_CAPABILITIES.get(model.strip().lower())


def supports_resolution(model: Any) -> bool:
    """모델이 ``resolution``을 받는지 여부. 모르는 슬러그는 받는다고 본다."""

    capability = model_capabilities(model)
    if capability is None:
        return True
    return bool(capability.resolutions)


def quality_for_image_size(model: Any, image_size: Any) -> str | None:
    """생성 이미지 크기를 모델이 받는 ``quality`` 등급으로 옮긴다.

    ``resolution``을 받는 모델이거나 옮길 등급이 없으면 ``None``을 돌려준다.
    UI가 "이 모델은 이미지 크기 대신 품질 등급을 쓴다"를 알리는 데 쓴다.
    """

    capability = model_capabilities(model)
    if capability is None or capability.resolutions or not capability.qualities:
        return None
    if not isinstance(image_size, str):
        return None
    for candidate in _IMAGE_SIZE_QUALITY_FALLBACKS.get(image_size.strip(), ()):
        if candidate in capability.qualities:
            return candidate
    return None


def clamp_quality(model: Any, quality: Any) -> str:
    """요청한 quality 등급을 모델이 실제로 받는 등급까지 낮춘다.

    max·xhigh·high·medium·low 순서에서 요청 등급 이하의 첫 지원 등급을 고른다.
    등급 이름 자체가 알려진 목록 밖이면 오타로 보고 거부한다.
    """

    value = _require_non_empty_text(quality, "이미지 품질 등급").strip()
    if value not in _KNOWN_QUALITIES:
        raise ValueError(
            f"이미지 품질 등급은 {', '.join(_KNOWN_QUALITIES)} 중 하나여야 합니다."
        )
    capability = model_capabilities(model)
    # 능력치를 모르는 슬러그와 quality를 쓰지 않는 모델은 값을 그대로 둔다.
    if capability is None or not capability.qualities or value in capability.qualities:
        return value
    # auto를 받지 않는 모델에서는 중간 등급부터 아래로 내려간다.
    ladder = _QUALITY_LADDER[_QUALITY_LADDER.index("high"):]
    if value in _QUALITY_LADDER:
        ladder = _QUALITY_LADDER[_QUALITY_LADDER.index(value):]
    for candidate in ladder:
        if candidate in capability.qualities:
            return candidate
    raise ValueError(
        f"{model} 모델의 이미지 품질 등급은 "
        f"{', '.join(capability.qualities)} 중 하나여야 합니다."
    )


def effective_image_size(model: Any, image_size: Any) -> str:
    """모델이 실제로 만들어 내는 이미지 크기 등급을 돌려준다.

    GPT 계열은 resolution을 받지 않고 결과가 1K급으로 고정되므로 항상 "1K"다.
    UI·파이프라인이 표시 크기를 모델에 맞춰 고정하는 데 쓴다.
    """

    value = _require_non_empty_text(image_size, "이미지 해상도").strip()
    if value not in RESOLUTION_OPTIONS:
        raise ValueError(f"이미지 해상도는 {', '.join(RESOLUTION_OPTIONS)} 중 하나여야 합니다.")
    capability = model_capabilities(model)
    # 능력치를 모르는 슬러그는 호출자가 고른 값을 그대로 쓴다.
    if capability is None:
        return value
    # resolution을 받지 않는 모델(GPT 계열)은 결과가 1K급으로 고정된다.
    if not capability.resolutions:
        return "1K"
    if value in capability.resolutions:
        return value
    for candidate in _RESOLUTION_LADDER[_RESOLUTION_LADDER.index(value):]:
        if candidate in capability.resolutions:
            return candidate
    # 요청보다 낮은 등급이 없으면 모델이 받는 가장 낮은 등급으로 올린다.
    return next(
        candidate
        for candidate in reversed(_RESOLUTION_LADDER)
        if candidate in capability.resolutions
    )


def _validate_image_mime_type(mime_type: Any) -> str:
    value = _require_non_empty_text(mime_type, "이미지 MIME 형식")
    if _IMAGE_MIME_PATTERN.fullmatch(value) is None:
        raise ValueError("이미지 MIME 형식은 안전한 image/* 값이어야 합니다.")
    return value


def _validate_base64_image(data_base64: Any) -> str:
    value = _require_non_empty_text(data_base64, "이미지 base64 데이터")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("이미지 base64 데이터가 올바르지 않습니다.") from error
    if not decoded:
        raise ValueError("이미지 base64 데이터가 비어 있습니다.")
    return value


def _data_url(image: Any) -> str:
    """``(MIME 형식, base64)`` 쌍을 OpenRouter가 받는 data URL로 바꾼다."""

    if not isinstance(image, (tuple, list)) or len(image) != 2:
        raise ValueError("입력 이미지는 (MIME 형식, base64 데이터) 쌍이어야 합니다.")
    mime_type = _validate_image_mime_type(image[0])
    data_base64 = _validate_base64_image(image[1])
    return f"data:{mime_type};base64,{data_base64}"


def build_request_headers(api_key: str) -> dict[str, str]:
    """OpenRouter 요청 공통 헤더를 만든다."""

    key = _require_non_empty_text(api_key, "OpenRouter API 키").strip()
    if "\r" in key or "\n" in key:
        raise ValueError("OpenRouter API 키에 줄바꿈을 포함할 수 없습니다.")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_URL,
        "X-Title": APP_TITLE,
    }


def build_analysis_payload(
    prompt: str,
    images: Sequence[tuple[str, str]],
    model: str = DEFAULT_ANALYSIS_MODEL,
) -> dict[str, Any]:
    """참조 이미지 분석용 ``/chat/completions`` JSON 본문을 만든다."""

    prompt_value = _require_non_empty_text(prompt, "분석 프롬프트")
    model_value = validate_model_slug(model)
    if not images:
        raise ValueError("분석할 이미지가 최소 한 장 필요합니다.")

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_value}]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": _data_url(image)}})

    return {
        "model": model_value,
        "messages": [{"role": "user", "content": content}],
        # 분석 결과는 항상 JSON 객체 하나여야 파싱 규칙이 단순해진다.
        "response_format": {"type": "json_object"},
    }


def extract_analysis_text(response: Mapping[str, Any]) -> str:
    """``/chat/completions`` 응답에서 assistant 텍스트를 순서대로 합친다."""

    if not isinstance(response, Mapping):
        raise ValueError("OpenRouter 응답은 JSON 객체여야 합니다.")
    error = response.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        raise RuntimeError(
            f"OpenRouter 오류: {message if isinstance(message, str) else error}"
        )

    choices = response.get("choices")
    if not isinstance(choices, list):
        return ""

    texts: list[str] = []
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content:
                texts.append(content)
            continue
        # 일부 모델은 content를 조각 배열로 돌려준다.
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "\n".join(texts)


def build_turnaround_payload(
    prompt: str,
    images: Sequence[tuple[str, str]],
    model: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    resolution: str = DEFAULT_RESOLUTION,
    quality: str | None = None,
) -> dict[str, Any]:
    """한 장의 3면도 생성용 ``/images`` JSON 본문을 만든다.

    첫 이미지는 모델 형상 contact sheet, 나머지는 스타일 참조다.
    ``quality``는 resolution을 받지 않는 모델에서만 쓰이며, 생략하면 이미지 크기에서
    등급을 옮겨 온다. resolution을 받는 모델에서는 무시한다.
    """

    prompt_value = _require_non_empty_text(prompt, "3면도 프롬프트")
    model_value = validate_model_slug(model)
    capability = model_capabilities(model_value)
    # 등재된 모델은 그 모델이 실제로 받는 목록으로, 사용자가 직접 넣은 슬러그는
    # 레이아웃 계약 전역 목록으로 검사한다.
    allowed_aspects = (
        capability.aspect_ratios if capability is not None else ASPECT_RATIO_OPTIONS
    )
    aspect_value = _require_non_empty_text(aspect_ratio, "이미지 종횡비").strip()
    if aspect_value not in allowed_aspects:
        raise ValueError(f"이미지 종횡비는 {', '.join(allowed_aspects)} 중 하나여야 합니다.")
    resolution_value = _require_non_empty_text(resolution, "이미지 해상도").strip()
    if resolution_value not in RESOLUTION_OPTIONS:
        raise ValueError(f"이미지 해상도는 {', '.join(RESOLUTION_OPTIONS)} 중 하나여야 합니다.")
    reference_limit = (
        capability.max_input_references if capability is not None else MAX_INPUT_REFERENCES
    )
    if not images:
        raise ValueError("모델 contact sheet가 최소 한 장 필요합니다.")
    if len(images) > reference_limit:
        raise ValueError(
            f"OpenRouter 참조 이미지는 최대 {reference_limit}장까지 사용할 수 있습니다."
        )

    payload: dict[str, Any] = {
        "model": model_value,
        "prompt": prompt_value,
        "input_references": [
            {"type": "image_url", "image_url": {"url": _data_url(image)}}
            for image in images
        ],
        "aspect_ratio": aspect_value,
        # 비용 계약: 한 번의 호출로 정확히 한 장만 만든다.
        "n": 1,
    }
    if capability is None or capability.resolutions:
        # 모델이 받지 않는 등급은 워커까지 가지 않고 여기서 한 단계씩 낮춘다.
        payload["resolution"] = effective_image_size(model_value, resolution_value)
        return payload

    # resolution을 받지 않는 모델(GPT 계열)은 호출자가 고른 quality 등급을 보내고,
    # 지정이 없으면 이미지 크기에서 등급을 옮겨 온다.
    if quality is None:
        quality_value = quality_for_image_size(model_value, resolution_value)
    else:
        quality_value = clamp_quality(model_value, quality)
    if quality_value is not None:
        payload["quality"] = quality_value
    return payload


def extract_image_response(response: Mapping[str, Any]) -> tuple[str, bytes]:
    """``/images`` 응답에서 정확히 한 장의 이미지를 엄격히 디코딩한다."""

    if not isinstance(response, Mapping):
        raise ValueError("OpenRouter Images 응답은 JSON 객체여야 합니다.")
    error = response.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        raise RuntimeError(
            f"OpenRouter 오류: {message if isinstance(message, str) else error}"
        )

    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("OpenRouter Images 응답에는 이미지가 정확히 한 장 있어야 합니다.")
    image = data[0]
    if not isinstance(image, Mapping):
        raise ValueError("OpenRouter Images 이미지 항목은 JSON 객체여야 합니다.")

    encoded = image.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("OpenRouter Images 응답에 b64_json 이미지가 없습니다.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("OpenRouter Images 응답의 b64_json이 올바르지 않습니다.") from error
    if not image_bytes:
        raise ValueError("OpenRouter Images 응답의 이미지 데이터가 비어 있습니다.")

    mime_type = image.get("media_type") or image.get("mime_type") or "image/png"
    mime_type = _validate_image_mime_type(mime_type)
    if mime_type not in _SUPPORTED_OUTPUT_MIME_TYPES:
        raise ValueError(f"지원하지 않는 결과 이미지 형식입니다: {mime_type}")
    return mime_type, image_bytes


__all__ = (
    "APP_TITLE",
    "APP_URL",
    "BASE_URL",
    "CHAT_COMPLETIONS_ENDPOINT",
    "DEFAULT_ANALYSIS_MODEL",
    "DEFAULT_ASPECT_RATIO",
    "DEFAULT_IMAGE_MODEL",
    "DEFAULT_RESOLUTION",
    "IMAGES_ENDPOINT",
    "IMAGE_MODEL_CAPABILITIES",
    "ImageModelCapability",
    "MAX_INPUT_REFERENCES",
    "RESOLUTION_OPTIONS",
    "build_analysis_payload",
    "build_request_headers",
    "build_turnaround_payload",
    "clamp_quality",
    "effective_image_size",
    "extract_analysis_text",
    "extract_image_response",
    "model_capabilities",
    "quality_for_image_size",
    "supports_resolution",
    "validate_model_slug",
)
