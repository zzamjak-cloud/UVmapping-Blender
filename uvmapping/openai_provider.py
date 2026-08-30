"""OpenAI 분석 및 이미지 편집 요청을 만드는 Blender 비의존 순수 함수."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any, Mapping, Sequence


DEFAULT_ANALYSIS_MODEL = "gpt-5.6"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_IMAGE_SIZE = "2352x1008"
DEFAULT_IMAGE_QUALITY = "high"

_MIN_IMAGE_PIXELS = 655_360
_MAX_IMAGE_PIXELS = 8_294_400
_MAX_IMAGE_EDGE = 3_840
_MAX_ASPECT_RATIO = 3.0
_SIZE_PATTERN = re.compile(r"^(\d+)x(\d+)$")
_IMAGE_MIME_PATTERN = re.compile(r"^image/[0-9A-Za-z!#$&'*+.^_`|~-]+$")
_BOUNDARY_PATTERN = re.compile(r"^[0-9A-Za-z!#$%&'*+.^_`|~-]+$")
_MIME_BY_OUTPUT_FORMAT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _require_non_empty_text(value: Any, label: str) -> str:
    """필수 문자열을 검사하고 원래 값을 보존한다."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}이(가) 비어 있습니다.")
    return value


def _validate_image_mime_type(mime_type: Any) -> str:
    """인라인 및 업로드 입력에 사용할 이미지 MIME 형식을 검사한다."""

    value = _require_non_empty_text(mime_type, "이미지 MIME 형식")
    if _IMAGE_MIME_PATTERN.fullmatch(value) is None:
        raise ValueError("이미지 MIME 형식은 안전한 image/* 값이어야 합니다.")
    return value


def _validate_base64_image(data_base64: Any) -> str:
    """Responses API에 넣을 이미지 base64가 엄격히 디코딩되는지 검사한다."""

    value = _require_non_empty_text(data_base64, "이미지 base64 데이터")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("이미지 base64 데이터가 올바르지 않습니다.") from error
    if not decoded:
        raise ValueError("이미지 base64 데이터가 비어 있습니다.")
    return value


def build_openai_analysis_payload(
    prompt: str,
    images: Sequence[tuple[str, str]],
    model: str = DEFAULT_ANALYSIS_MODEL,
) -> dict[str, Any]:
    """gpt-5.6 Responses API용 다중 이미지 분석 JSON payload를 만든다."""

    prompt_value = _require_non_empty_text(prompt, "분석 프롬프트")
    model_value = _require_non_empty_text(model, "분석 모델")
    if not images:
        raise ValueError("분석할 이미지가 최소 한 장 필요합니다.")

    content: list[dict[str, str]] = [{"type": "input_text", "text": prompt_value}]
    for image in images:
        if not isinstance(image, (tuple, list)) or len(image) != 2:
            raise ValueError("분석 이미지는 (MIME 형식, base64 데이터) 쌍이어야 합니다.")
        mime_type = _validate_image_mime_type(image[0])
        data_base64 = _validate_base64_image(image[1])
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{data_base64}",
            }
        )

    return {
        "model": model_value,
        "input": [{"role": "user", "content": content}],
    }


def extract_openai_response_text(response: Mapping[str, Any]) -> str:
    """Responses API의 모든 output_text 조각을 응답 순서대로 합친다."""

    if not isinstance(response, Mapping):
        raise ValueError("OpenAI Responses 응답은 JSON 객체여야 합니다.")

    output = response.get("output")
    if not isinstance(output, list):
        return ""

    texts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def validate_gpt_image_2_size(size: str) -> tuple[int, int]:
    """GPT-Image-2 임의 해상도 제약을 검사하고 너비와 높이를 반환한다."""

    size_value = _require_non_empty_text(size, "이미지 해상도")
    match = _SIZE_PATTERN.fullmatch(size_value)
    if match is None:
        raise ValueError("이미지 해상도는 WIDTHxHEIGHT 형식이어야 합니다.")

    width, height = (int(value) for value in match.groups())
    if width <= 0 or height <= 0:
        raise ValueError("GPT-Image-2 이미지의 양 변은 0보다 커야 합니다.")
    if width % 16 != 0 or height % 16 != 0:
        raise ValueError("GPT-Image-2 이미지의 양 변은 16의 배수여야 합니다.")
    if max(width, height) > _MAX_IMAGE_EDGE:
        raise ValueError("GPT-Image-2 이미지의 최대 변은 3840px 이하여야 합니다.")
    if max(width, height) / min(width, height) > _MAX_ASPECT_RATIO:
        raise ValueError("GPT-Image-2 이미지의 장단변 비율은 3:1 이하여야 합니다.")

    pixel_count = width * height
    if pixel_count < _MIN_IMAGE_PIXELS or pixel_count > _MAX_IMAGE_PIXELS:
        raise ValueError(
            "GPT-Image-2 이미지의 총 픽셀 수는 655360~8294400 범위여야 합니다."
        )
    return width, height


def _quote_multipart_value(value: str, label: str) -> str:
    """multipart 헤더 삽입을 막으면서 quoted-string 값을 보존한다."""

    text = _require_non_empty_text(value, label)
    if "\r" in text or "\n" in text:
        raise ValueError(f"{label}에 줄바꿈을 포함할 수 없습니다.")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    """UTF-8 일반 multipart 필드 한 개를 직렬화한다."""

    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n'
        "\r\n"
    ).encode("ascii")
    return header + value.encode("utf-8") + b"\r\n"


def _default_boundary(
    prompt: str,
    images: Sequence[tuple[str, str, bytes]],
) -> str:
    """전역 상태 없이 입력으로부터 충돌 가능성이 낮은 boundary를 만든다."""

    digest = hashlib.sha256(prompt.encode("utf-8"))
    for filename, mime_type, image_data in images:
        digest.update(filename.encode("utf-8"))
        digest.update(mime_type.encode("ascii"))
        digest.update(image_data)
    return f"----UVMappingOpenAI{digest.hexdigest()[:24]}"


def build_openai_image_edit_multipart(
    prompt: str,
    images: Sequence[tuple[str, str, bytes]],
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = DEFAULT_IMAGE_SIZE,
    quality: str = DEFAULT_IMAGE_QUALITY,
    boundary: str | None = None,
    background: str = "opaque",
) -> tuple[str, bytes]:
    """GPT-Image-2 `/v1/images/edits`용 multipart/form-data 본문을 만든다."""

    prompt_value = _require_non_empty_text(prompt, "이미지 생성 프롬프트")
    model_value = _require_non_empty_text(model, "이미지 모델")
    validate_gpt_image_2_size(size)
    if quality not in {"low", "medium", "high", "auto"}:
        raise ValueError("이미지 품질은 low, medium, high, auto 중 하나여야 합니다.")
    if background not in {"opaque", "auto"}:
        raise ValueError("배경은 opaque 또는 auto만 사용할 수 있습니다.")
    if not images:
        raise ValueError("이미지 편집 입력이 최소 한 장 필요합니다.")

    normalized_images: list[tuple[str, str, bytes]] = []
    for image in images:
        if not isinstance(image, (tuple, list)) or len(image) != 3:
            raise ValueError(
                "편집 이미지는 (파일 이름, MIME 형식, 바이너리 데이터) 순서여야 합니다."
            )
        filename = _require_non_empty_text(image[0], "이미지 파일 이름")
        mime_type = _validate_image_mime_type(image[1])
        image_data = image[2]
        if not isinstance(image_data, bytes) or not image_data:
            raise ValueError("편집 이미지 데이터는 비어 있지 않은 bytes여야 합니다.")
        normalized_images.append((filename, mime_type, image_data))

    boundary_value = boundary or _default_boundary(prompt_value, normalized_images)
    boundary_value = _require_non_empty_text(boundary_value, "multipart boundary")
    try:
        boundary_value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("multipart boundary는 ASCII 문자만 사용할 수 있습니다.") from error
    if len(boundary_value) > 70 or _BOUNDARY_PATTERN.fullmatch(boundary_value) is None:
        raise ValueError("multipart boundary 형식이 올바르지 않습니다.")

    delimiter = f"\r\n--{boundary_value}".encode("ascii")
    if delimiter in prompt_value.encode("utf-8") or delimiter in model_value.encode("utf-8"):
        raise ValueError("텍스트 필드가 multipart boundary와 충돌합니다.")

    parts = [
        _multipart_field(boundary_value, "model", model_value),
        _multipart_field(boundary_value, "prompt", prompt_value),
        _multipart_field(boundary_value, "n", "1"),
        _multipart_field(boundary_value, "size", size),
        _multipart_field(boundary_value, "quality", quality),
        _multipart_field(boundary_value, "output_format", "png"),
        _multipart_field(boundary_value, "background", background),
    ]
    for filename, mime_type, image_data in normalized_images:
        safe_filename = _quote_multipart_value(filename, "이미지 파일 이름")
        file_header = (
            f"--{boundary_value}\r\n"
            f'Content-Disposition: form-data; name="image[]"; filename="{safe_filename}"\r\n'
            f"Content-Type: {mime_type}\r\n"
            "\r\n"
        ).encode("utf-8")
        parts.append(file_header + image_data + b"\r\n")
    parts.append(f"--{boundary_value}--\r\n".encode("ascii"))

    body = b"".join(parts)
    for _, _, image_data in normalized_images:
        if delimiter in image_data:
            raise ValueError("이미지 데이터가 multipart boundary와 충돌합니다.")
    return f"multipart/form-data; boundary={boundary_value}", body


def extract_openai_image_response(
    response: Mapping[str, Any],
    output_format: str = "png",
) -> tuple[str, bytes]:
    """Images API 응답에서 정확히 한 장의 base64 이미지를 엄격히 디코딩한다."""

    if not isinstance(response, Mapping):
        raise ValueError("OpenAI Images 응답은 JSON 객체여야 합니다.")
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("OpenAI Images 응답에는 이미지가 정확히 한 장 있어야 합니다.")
    image = data[0]
    if not isinstance(image, Mapping):
        raise ValueError("OpenAI Images 이미지 항목은 JSON 객체여야 합니다.")

    encoded = image.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("OpenAI Images 응답에 b64_json 이미지가 없습니다.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("OpenAI Images 응답의 b64_json이 올바르지 않습니다.") from error
    if not image_bytes:
        raise ValueError("OpenAI Images 응답의 이미지 데이터가 비어 있습니다.")

    mime_type = _MIME_BY_OUTPUT_FORMAT.get(output_format)
    if mime_type is None:
        raise ValueError("출력 형식은 png, jpeg, webp 중 하나여야 합니다.")
    return mime_type, image_bytes
