"""OpenRouter 분석·이미지 생성 요청을 만드는 Blender 비의존 순수 함수.

두 Provider를 각자의 API로 호출하던 구조를 OpenRouter 한 곳으로 합쳤다.
참조 분석은 OpenAI 호환 ``/chat/completions``, 3면도 생성은 ``/images``를 쓰며
어떤 모델을 고르든 요청·응답 형식은 동일하다.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Mapping, Sequence


BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_ENDPOINT = "chat/completions"
IMAGES_ENDPOINT = "images"

DEFAULT_ANALYSIS_MODEL = "google/gemini-3.7-flash"
DEFAULT_IMAGE_MODEL = "google/gemini-3-pro-image"
DEFAULT_ASPECT_RATIO = "21:9"
DEFAULT_RESOLUTION = "2K"

# 애드온 사용량을 OpenRouter 대시보드에서 구분하기 위한 선택적 출처 헤더.
APP_TITLE = "UV Mapping Blender"
APP_URL = "https://github.com/zzamjak-cloud/UVmapping-Blender"

# OpenRouter가 image-to-image 참조로 허용하는 최대 장수.
MAX_INPUT_REFERENCES = 16

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
) -> dict[str, Any]:
    """한 장의 3면도 생성용 ``/images`` JSON 본문을 만든다.

    첫 이미지는 모델 형상 contact sheet, 나머지는 스타일 참조다.
    """

    prompt_value = _require_non_empty_text(prompt, "3면도 프롬프트")
    model_value = validate_model_slug(model)
    aspect_value = _require_non_empty_text(aspect_ratio, "이미지 종횡비")
    resolution_value = _require_non_empty_text(resolution, "이미지 해상도")
    if not images:
        raise ValueError("모델 contact sheet가 최소 한 장 필요합니다.")
    if len(images) > MAX_INPUT_REFERENCES:
        raise ValueError(
            f"OpenRouter 참조 이미지는 최대 {MAX_INPUT_REFERENCES}장까지 사용할 수 있습니다."
        )

    return {
        "model": model_value,
        "prompt": prompt_value,
        "input_references": [
            {"type": "image_url", "image_url": {"url": _data_url(image)}}
            for image in images
        ],
        "aspect_ratio": aspect_value,
        "resolution": resolution_value,
        # 비용 계약: 한 번의 호출로 정확히 한 장만 만든다.
        "n": 1,
    }


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
    "MAX_INPUT_REFERENCES",
    "build_analysis_payload",
    "build_request_headers",
    "build_turnaround_payload",
    "extract_analysis_text",
    "extract_image_response",
    "validate_model_slug",
)
