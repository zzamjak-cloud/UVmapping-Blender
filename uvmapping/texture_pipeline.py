"""AI 손맵 텍스처 파이프라인의 Blender 비의존 데이터 계약과 순수 함수."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


ANALYSIS_SCHEMA_VERSION = "1.0"
TURNAROUND_VIEWS = ("FRONT", "RIGHT", "BACK")
DEFAULT_IMAGE_MODEL = "google/gemini-3-pro-image"
DEFAULT_ASPECT_RATIO = "21:9"
DEFAULT_IMAGE_SIZE = "2K"
MAX_REFERENCE_IMAGE_BYTES = 32 * 1024 * 1024
REFERENCE_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def validate_reference_image_path(path: str | Path) -> tuple[Path, str]:
    """외부 Provider로 전송하기 전에 참조 이미지 파일의 신뢰 경계를 검사한다."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"심볼릭 링크 참조 이미지는 사용할 수 없습니다: {candidate}")
    if not candidate.is_file():
        raise ValueError(f"참조 이미지를 찾을 수 없습니다: {candidate}")
    mime_type = REFERENCE_IMAGE_MIME_TYPES.get(candidate.suffix.lower())
    if mime_type is None:
        raise ValueError("참조 이미지는 JPG, PNG 또는 WebP 파일만 사용할 수 있습니다.")
    size = candidate.stat().st_size
    if size <= 0 or size > MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("참조 이미지 크기는 0바이트 초과 32MB 이하여야 합니다.")

    data = candidate.read_bytes()
    if mime_type == "image/png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    elif mime_type == "image/jpeg":
        valid = data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    else:
        valid = (
            len(data) >= 12
            and data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") == len(data) - 8
        )
    if not valid:
        raise ValueError(f"파일 내용이 올바른 {mime_type} 이미지가 아닙니다: {candidate.name}")
    return candidate, mime_type


@dataclass(frozen=True, slots=True)
class ReferenceRole:
    """각 참조 이미지에서 신뢰할 수 있는 정보의 역할."""

    reference_index: int
    role: str
    use_for: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class StyleDescription:
    """여러 참조 이미지에 공통으로 나타나는 손맵 스타일."""

    art_style: str
    brushwork: str
    detail_density: str
    outline_style: str
    color_palette: tuple[str, ...]
    shading_style: str
    edge_highlight: str


@dataclass(frozen=True, slots=True)
class SurfaceRegion:
    """모델의 특정 영역에 적용할 시각적 특징."""

    name: str
    location: str
    base_colors: tuple[str, ...]
    material_cues: tuple[str, ...]
    details: tuple[str, ...]
    confidence: float
    source: str


@dataclass(frozen=True, slots=True)
class DesignRules:
    """생성 결과에서 유지하거나 배제할 디자인 규칙."""

    preserve: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisUncertainty:
    """관찰 사실과 추론 및 참조 간 충돌을 분리한 기록."""

    observed: tuple[str, ...]
    inferred: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceAnalysis:
    """자유 형식 참조 이미지 분석의 정규화된 결과."""

    schema_version: str
    object_summary: str
    reference_roles: tuple[ReferenceRole, ...]
    style: StyleDescription
    surface_regions: tuple[SurfaceRegion, ...]
    design_rules: DesignRules
    uncertainty: AnalysisUncertainty

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화 가능한 사전으로 변환한다."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class InlineImage:
    """Provider에 전달할 base64 인라인 이미지."""

    mime_type: str
    data_base64: str
    role: str = "reference"
    name: str = ""

    def __post_init__(self) -> None:
        if not self.mime_type.startswith("image/"):
            raise ValueError("인라인 이미지는 image/* MIME 형식이어야 합니다.")
        if not self.data_base64.strip():
            raise ValueError("인라인 이미지 데이터가 비어 있습니다.")


@dataclass(frozen=True, slots=True)
class TurnaroundImageRequest:
    """한 번의 호출로 한 장의 3면도만 생성하는 Provider 중립 요청."""

    prompt: str
    contact_sheet: InlineImage
    reference_images: tuple[InlineImage, ...]
    model: str = DEFAULT_IMAGE_MODEL
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    image_size: str = DEFAULT_IMAGE_SIZE
    views: tuple[str, str, str] = TURNAROUND_VIEWS
    provider_call_count: int = 1
    output_image_count: int = 1

    def __post_init__(self) -> None:
        if self.views != TURNAROUND_VIEWS:
            raise ValueError("3면도 순서는 FRONT, RIGHT, BACK으로 고정됩니다.")
        if self.provider_call_count != 1 or self.output_image_count != 1:
            raise ValueError("비용 절감을 위해 Provider 1회 호출과 결과 1장만 허용합니다.")
        if self.contact_sheet.role != "geometry_contact_sheet":
            raise ValueError("실제 모델 contact sheet의 역할이 올바르지 않습니다.")
        # 참조 이미지는 선택 사항이다. 없으면 프롬프트만으로 스타일을 정한다.


def build_reference_analysis_prompt(reference_count: int) -> str:
    """다중 참조에서 공통 스타일을 추출하는 엄격한 JSON 프롬프트를 만든다."""

    if reference_count < 1:
        raise ValueError("참조 이미지가 최소 한 장 필요합니다.")
    return f"""당신은 캐주얼 게임용 스타일리시 손맵 텍스처 아트 디렉터입니다.
입력된 참조 이미지 {reference_count}장을 함께 분석하세요. 이미지는 같은 물체의 다른 면이 아닐 수 있으며,
각 이미지의 유용한 역할과 여러 이미지에서 반복되는 공통 스타일만 추출해야 합니다.

규칙:
- 이미지에 직접 보이는 사실은 uncertainty.observed에, 보이지 않아 추론한 내용은 uncertainty.inferred에 기록합니다.
- 참조끼리 모순되는 내용은 임의로 합치지 말고 uncertainty.conflicts에 기록합니다.
- 로고, 워터마크, 사진 배경, UI, 텍스트는 디자인 특징으로 복제하지 않습니다.
- 자세, 포즈, 손동작, 시선, 카메라 각도, 구도, 실루엣, 비율, 체형은 기록하지 않습니다. 이 값들은 텍스처를 입힐 3D 모델에서 이미 정해져 있으며, 참조에서 가져오면 결과가 어긋납니다.
- 재질의 물리적 PBR 값이 아니라 diffuse/albedo에 그릴 색, 붓질, 명암, 가장자리 강조를 설명합니다.
- Markdown, 코드펜스, 설명문 없이 아래 스키마와 정확히 같은 JSON 객체 하나만 반환합니다.
- 모든 배열은 정보가 없어도 빈 배열로 포함하고, confidence는 0.0~1.0 숫자로 반환합니다.

{{
  "schema_version": "{ANALYSIS_SCHEMA_VERSION}",
  "object_summary": "짧은 물체 및 디자인 요약(자세와 구도는 빼고 색·재질 중심으로)",
  "reference_roles": [
    {{"reference_index": 0, "role": "역할", "use_for": ["색상", "붓질"], "confidence": 0.0}}
  ],
  "style": {{
    "art_style": "스타일",
    "brushwork": "붓질",
    "detail_density": "디테일 밀도",
    "outline_style": "윤곽선",
    "color_palette": ["#RRGGBB 또는 색 이름"],
    "shading_style": "손으로 그린 명암 방식",
    "edge_highlight": "모서리 강조 방식"
  }},
  "surface_regions": [
    {{
      "name": "영역 이름",
      "location": "위치",
      "base_colors": ["색상"],
      "material_cues": ["그림으로 표현할 재질 단서"],
      "details": ["무늬 또는 마모"],
      "confidence": 0.0,
      "source": "observed 또는 inferred"
    }}
  ],
  "design_rules": {{"preserve": ["유지할 특징"], "exclude": ["배제할 특징"]}},
  "uncertainty": {{"observed": ["직접 관찰"], "inferred": ["추론"], "conflicts": ["충돌"]}}
}}"""


def _extract_json_object(raw_text: str) -> str:
    """앞뒤 잡음과 코드펜스가 있어도 첫 번째 균형 잡힌 JSON 객체를 찾는다."""

    text = raw_text.lstrip("\ufeff").strip()
    fence_matches = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates = [*fence_matches, text]
    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            character = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return candidate[start : index + 1]
    raise ValueError("응답에서 JSON 객체를 찾을 수 없습니다.")


def _remove_trailing_commas(raw_json: str) -> str:
    """문자열 내부는 보존하면서 객체와 배열 끝의 trailing comma만 제거한다."""

    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(raw_json):
        character = raw_json[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == ",":
            next_index = index + 1
            while next_index < len(raw_json) and raw_json[next_index].isspace():
                next_index += 1
            if next_index < len(raw_json) and raw_json[next_index] in "}]":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(text for item in value if (text := _text(item)))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def normalize_reference_analysis(payload: Mapping[str, Any]) -> ReferenceAnalysis:
    """알 수 없는 필드를 버리고 예상 타입과 허용값으로 분석 결과를 정규화한다."""

    if not isinstance(payload, Mapping):
        raise ValueError("분석 결과의 최상위 값은 JSON 객체여야 합니다.")
    style = _mapping(payload.get("style"))
    rules = _mapping(payload.get("design_rules"))
    uncertainty = _mapping(payload.get("uncertainty"))

    reference_roles: list[ReferenceRole] = []
    raw_roles = payload.get("reference_roles")
    if isinstance(raw_roles, (list, tuple)):
        for position, item in enumerate(raw_roles):
            role = _mapping(item)
            raw_index = role.get("reference_index", position)
            try:
                reference_index = max(0, int(raw_index))
            except (TypeError, ValueError):
                reference_index = position
            reference_roles.append(
                ReferenceRole(
                    reference_index=reference_index,
                    role=_text(role.get("role")),
                    use_for=_text_tuple(role.get("use_for")),
                    confidence=_confidence(role.get("confidence")),
                )
            )

    surface_regions: list[SurfaceRegion] = []
    raw_regions = payload.get("surface_regions")
    if isinstance(raw_regions, (list, tuple)):
        for item in raw_regions:
            region = _mapping(item)
            source = _text(region.get("source")).lower()
            if source not in {"observed", "inferred"}:
                source = "inferred"
            surface_regions.append(
                SurfaceRegion(
                    name=_text(region.get("name")),
                    location=_text(region.get("location")),
                    base_colors=_text_tuple(region.get("base_colors")),
                    material_cues=_text_tuple(region.get("material_cues")),
                    details=_text_tuple(region.get("details")),
                    confidence=_confidence(region.get("confidence")),
                    source=source,
                )
            )

    return ReferenceAnalysis(
        schema_version=ANALYSIS_SCHEMA_VERSION,
        object_summary=_text(payload.get("object_summary")),
        reference_roles=tuple(reference_roles),
        style=StyleDescription(
            art_style=_text(style.get("art_style")),
            brushwork=_text(style.get("brushwork")),
            detail_density=_text(style.get("detail_density")),
            outline_style=_text(style.get("outline_style")),
            color_palette=_text_tuple(style.get("color_palette")),
            shading_style=_text(style.get("shading_style")),
            edge_highlight=_text(style.get("edge_highlight")),
        ),
        surface_regions=tuple(surface_regions),
        design_rules=DesignRules(
            preserve=_text_tuple(rules.get("preserve")),
            exclude=_text_tuple(rules.get("exclude")),
        ),
        uncertainty=AnalysisUncertainty(
            observed=_text_tuple(uncertainty.get("observed")),
            inferred=_text_tuple(uncertainty.get("inferred")),
            conflicts=_text_tuple(uncertainty.get("conflicts")),
        ),
    )


def parse_reference_analysis(raw_text: str) -> ReferenceAnalysis:
    """모델의 느슨한 텍스트 응답을 정규화된 참조 분석으로 변환한다."""

    try:
        payload = json.loads(_remove_trailing_commas(_extract_json_object(raw_text)))
    except json.JSONDecodeError as error:
        raise ValueError(f"참조 분석 JSON을 해석할 수 없습니다: {error.msg}") from error
    if not isinstance(payload, Mapping):
        raise ValueError("참조 분석 JSON은 객체여야 합니다.")
    return normalize_reference_analysis(payload)


def compile_turnaround_prompt(
    analysis: ReferenceAnalysis | Mapping[str, Any] | None,
    user_prompt: str = "",
    *,
    reference_image_count: int = 0,
) -> str:
    """모델 형상과 참조 스타일(또는 프롬프트만)을 한 장의 3면도에 결합하도록 지시한다.

    ``analysis``가 ``None``이면 참조 이미지 없이 사용자 지시만으로 스타일을
    정하는 프롬프트를 만든다. 이때 사용자 지시는 비어 있으면 안 된다.
    """

    instruction = user_prompt.strip() or "추가 지시 없음"
    if reference_image_count and analysis is None:
        raise ValueError("참조 이미지를 함께 보낼 때는 참조 분석이 필요합니다.")
    if analysis is None:
        if not user_prompt.strip():
            raise ValueError("참조 분석이 없으면 사용자 지시가 필요합니다.")
        role_section = (
            "- 첫 번째 이미지(role=geometry_contact_sheet): Blender의 실제 모델 형상입니다. "
            "실루엣, 비율, 부품 배치를 반드시 이 이미지에 맞춥니다. 이 외의 입력 이미지는 없습니다."
        )
        style_section = (
            "스타일 근거:\n"
            "- 참조 이미지가 없습니다. 아래 사용자 지시를 색, 재질 표현, 분위기의 유일한 근거로 사용합니다.\n"
            "- 지시가 다루지 않는 부분은 깔끔한 캐주얼 게임 손맵 스타일로 절제해 표현합니다."
        )
    else:
        normalized = (
            analysis
            if isinstance(analysis, ReferenceAnalysis)
            else normalize_reference_analysis(analysis)
        )
        # reference_roles와 uncertainty는 분석 단계의 기록일 뿐이고, 참조의
        # 서술이 생성 형상으로 새는 통로가 된다. 스타일 근거만 싣는다.
        analysis_payload = {
            key: value
            for key, value in normalized.to_dict().items()
            if key in ("object_summary", "style", "surface_regions", "design_rules")
        }
        analysis_json = json.dumps(
            analysis_payload, ensure_ascii=False, separators=(",", ":")
        )
        if reference_image_count:
            role_section = (
                "- 첫 번째 이미지(role=geometry_contact_sheet): Blender의 실제 모델 형상입니다. "
                "실루엣, 비율, 자세, 부품 배치를 반드시 이 이미지에 맞춥니다.\n"
                f"- 두 번째 이후 이미지 {reference_image_count}장(role=palette_only): 색과 재질 표현만 참고합니다. "
                "이 이미지의 캐릭터, 비율, 자세, 실루엣, 부품 구성은 절대 가져오지 않습니다."
            )
        else:
            role_section = (
                "- 입력 이미지는 첫 번째 한 장뿐입니다(role=geometry_contact_sheet). "
                "Blender의 실제 모델 형상이며, 실루엣, 비율, 자세, 부품 배치를 반드시 이 이미지에 맞춥니다.\n"
                "- 스타일은 아래 분석 JSON 텍스트만 근거로 합니다."
            )
        style_section = f"분석 JSON:\n{analysis_json}"
    return f"""캐주얼 게임용 스타일리시 손맵 diffuse/albedo 제작을 위한 3면도 한 장을 생성하세요.

입력 이미지 역할:
{role_section}

형상 계약(다른 모든 지시보다 우선):
- 첫 번째 이미지는 흰 배경 위의 회색 3D 모델입니다. 그 실루엣 안쪽을 색으로 채우는 작업이며, 실루엣 밖에는 아무것도 그리지 않습니다.
- 첫 번째 이미지의 실루엣, 비율, 크기, 화면 안 위치를 그대로 유지합니다. 이 형상은 텍스처를 입힐 실제 3D 모델이므로 한 픽셀도 재해석하지 않습니다.
- 첫 번째 이미지에 없는 부품(가방, 소품, 장비, 장식)은 추가하지 않고, 있는 부품을 빼지도 않습니다.
- 다른 참조나 분석 결과가 이 계약과 충돌하면 언제나 첫 번째 이미지를 따릅니다.

{style_section}

사용자 한 줄 지시:
{instruction}

출력 계약:
- Provider 호출 한 번에서 최종 이미지 정확히 한 장만 생성합니다. 시점별로 세 번 생성하지 마세요.
- 하나의 21:9 캔버스를 같은 너비의 3열로 나눕니다. 첫 번째 입력 이미지가 정확히 같은 21:9 3열 배치이므로 그 레이아웃을 그대로 따릅니다.
- 왼쪽부터 FRONT | RIGHT SIDE | BACK 순서이며, 세 칸에 동일한 물체, 동일한 축척, 동일한 세로 중심을 배치합니다.
- 각 열의 중앙 정사각형 viewport 안에 물체를 배치하고, 세 viewport의 상하좌우 여백을 동일하게 유지합니다.
- 첫 번째 입력 이미지의 실루엣 위에 색만 덧입히듯, 각 열에서 물체의 외곽선 위치, 크기, 세로 중심을 입력과 최대한 일치시킵니다. 부위 경계(예: 장갑과 소매, 신발과 바지)도 입력 실루엣의 같은 높이에 맞춥니다.
- 세 시점의 색, 무늬, 마모, 부품 연결은 서로 연속되고 일관되어야 합니다.
- 원근을 제거한 orthographic view처럼 표현하고 물체가 잘리지 않게 충분한 여백을 둡니다.
- 조명 사진이나 렌더가 아니라 diffuse/albedo에 옮길 수 있는 손으로 그린 색과 명암을 표현합니다.
- 배경은 투명 또는 완전히 균일한 단색으로 만듭니다.
- 텍스트, 라벨, 구분선, 숫자, 로고, 워터마크, 받침대, 그림자는 넣지 않습니다.
- 관찰되지 않은 뒷면은 주어진 근거(분석 JSON 또는 사용자 지시)와 일관되게 절제해 표현하며 새 부품을 임의로 만들지 않습니다."""


def build_turnaround_request(
    contact_sheet: InlineImage,
    reference_images: Sequence[InlineImage],
    analysis: ReferenceAnalysis | Mapping[str, Any] | None,
    user_instruction: str = "",
    *,
    model: str = DEFAULT_IMAGE_MODEL,
) -> TurnaroundImageRequest:
    """비용 계약이 고정된 단일 3면도 이미지 요청을 만든다."""

    return TurnaroundImageRequest(
        prompt=compile_turnaround_prompt(
            analysis, user_instruction, reference_image_count=len(reference_images)
        ),
        contact_sheet=contact_sheet,
        reference_images=tuple(reference_images),
        model=model,
    )
