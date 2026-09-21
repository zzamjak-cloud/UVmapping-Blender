"""AI 생성 실패 메시지를 사용자가 이해할 수 있는 원인으로 분류하는 Blender 비의존 순수 모듈.

상태 줄 한 줄에는 Provider가 돌려준 영문 원문이 잘려 들어가 "왜 실패했는지"가 남지
않는다. 여기서 원문을 종류별로 나누고 한국어 설명과 해결 방법을 붙여, 패널과 팝업이
같은 문구를 쓰게 한다.
"""

from __future__ import annotations

import re
from typing import NamedTuple


CONTENT_POLICY = "CONTENT_POLICY"
AUTH = "AUTH"
RATE_LIMIT = "RATE_LIMIT"
BAD_REQUEST = "BAD_REQUEST"
NETWORK = "NETWORK"
NO_IMAGE = "NO_IMAGE"
SERVER = "SERVER"
LOCAL = "LOCAL"
UNKNOWN = "UNKNOWN"

# 응답 본문이 길어도 팝업 한 화면에 들어오게 자른다.
MAX_RAW_PREVIEW = 300
# 팝업 label 한 줄이 감당하는 글자 수와 최대 줄 수. 넘치면 복사 버튼으로 안내한다.
RAW_LINE_WIDTH = 90
MAX_RAW_LINES = 4
RAW_OVERFLOW_HINT = "…(생략) 전체 원문은 '원문 복사' 버튼으로 가져가세요"

_REQUEST_ID_PATTERN = re.compile(r"\breq_[A-Za-z0-9]+")
# 작업자가 만드는 "OpenRouter API 오류(400): …" 형식에서만 상태 코드를 읽는다.
# 본문에 섞인 숫자를 상태 코드로 오해하지 않기 위해서다.
_STATUS_PATTERN = re.compile(r"오류\((\d{3})\)")


class FailureInfo(NamedTuple):
    """분류된 실패 한 건."""

    kind: str
    title: str
    explanation: str
    suggestions: tuple[str, ...]
    raw: str = ""
    request_id: str = ""


_DESCRIPTIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    CONTENT_POLICY: (
        "모델 제공사의 안전 시스템이 요청을 거부했습니다",
        "프롬프트나 참조 이미지가 콘텐츠 정책에 걸려 이미지 생성이 시작되지도 못했습니다. "
        "요청 내용을 바꾸지 않으면 같은 결과가 반복됩니다.",
        (
            "유명 캐릭터·브랜드·실존 인물 이름을 프롬프트에서 빼고 외형(복장·색·비율·분위기)으로만 묘사하세요.",
            "공식 아트 대신 직접 그린 자체 원화를 참조 이미지로 사용하세요.",
            "모델을 바꿔도 같은 입력이면 다시 막힐 수 있습니다. 모델보다 입력을 먼저 고치세요.",
            "폭력·선정적 표현으로 읽힐 수 있는 단어를 덜어 내고 다시 시도하세요.",
        ),
    ),
    AUTH: (
        "API 키 인증에 실패했습니다",
        "OpenRouter가 키를 받아들이지 않았습니다. 키가 없거나, 만료됐거나, 그 모델을 쓸 권한이 없습니다.",
        (
            "애드온 환경 설정에서 OpenRouter API 키를 다시 입력하세요.",
            "키 앞뒤에 공백이나 줄바꿈이 섞이지 않았는지 확인하세요.",
            "OpenRouter 대시보드에서 키가 살아 있는지, 해당 모델 사용이 허용되는지 확인하세요.",
        ),
    ),
    RATE_LIMIT: (
        "요청 한도에 걸렸습니다",
        "짧은 시간에 요청이 몰렸거나 계정 크레딧·한도가 소진됐습니다.",
        (
            "잠시 기다린 뒤 다시 시도하세요.",
            "OpenRouter 대시보드에서 남은 크레딧과 한도를 확인하세요.",
            "품질 프리셋을 낮춰 한 번에 보내는 호출 수를 줄이세요.",
        ),
    ),
    BAD_REQUEST: (
        "요청 형식이 거부됐습니다",
        "모델이 받지 못하는 파라미터나 값이 섞여 있습니다.",
        (
            "모델 슬러그가 정확한지, 이미지 생성 모델이 맞는지 확인하세요.",
            "생성 이미지 크기와 품질 단계를 기본값으로 되돌리고 다시 시도하세요.",
            "참조 이미지 수와 형식(PNG·JPEG·WebP)이 허용 범위인지 확인하세요.",
        ),
    ),
    NETWORK: (
        "네트워크 연결에 실패했습니다",
        "OpenRouter에 접속하지 못했습니다. 인터넷 연결, 프록시·방화벽, Blender 온라인 접근 설정 중 하나가 원인입니다.",
        (
            "인터넷 연결을 확인하고 다시 시도하세요.",
            "Blender 환경 설정 > 시스템에서 온라인 접근이 허용돼 있는지 확인하세요.",
            "VPN이나 프록시를 끄고 다시 시도하세요.",
        ),
    ),
    NO_IMAGE: (
        "모델이 이미지를 돌려주지 않았습니다",
        "응답에 이미지가 없습니다. 모델이 그림 대신 글로 답했거나 생성이 중간에 멈춘 경우이며, "
        "대개 콘텐츠 정책 거부가 완곡하게 나타난 형태입니다.",
        (
            "아래 원문에 모델이 남긴 거부 사유가 있는지 확인하세요.",
            "유명 캐릭터·브랜드·실존 인물 이름을 빼고 외형으로만 묘사해 다시 시도하세요.",
            "고른 모델이 이미지 생성 모델이 맞는지 확인하세요.",
        ),
    ),
    SERVER: (
        "모델 제공사 서버 오류입니다",
        "제공사 쪽에서 일시적으로 실패했습니다. 요청 내용 문제가 아닐 수 있습니다.",
        (
            "잠시 뒤 다시 시도하세요.",
            "계속 실패하면 다른 모델 프리셋으로 바꿔 보세요.",
            "OpenRouter 상태 페이지에서 장애 공지를 확인하세요.",
        ),
    ),
    LOCAL: (
        "애드온 내부 처리에서 실패했습니다",
        "AI 호출 자체가 아니라 파일 저장, 베이크, 검증 같은 로컬 단계에서 막혔습니다.",
        (
            "출력 경로에 쓰기 권한이 있는지, 디스크 여유가 있는지 확인하세요.",
            "대상 객체에 활성 UV 맵이 있는지 확인하세요.",
            "`생성 상태 초기화` 후 다시 생성하세요.",
        ),
    ),
    UNKNOWN: (
        "원인을 분류하지 못했습니다",
        "알려진 실패 유형에 들어맞지 않습니다. 아래 원문을 확인해 주세요.",
        (
            "원문을 복사해 검색하거나 문의에 첨부하세요.",
            "잠시 뒤 같은 설정으로 다시 시도해 재현되는지 확인하세요.",
            "모델 프리셋이나 참조 이미지를 바꿔 다시 시도하세요.",
        ),
    ),
}

# 앞에 있는 종류가 먼저 이긴다. 정책 거부 문구는 400 응답 안에 들어오므로
# 상태 코드 판정보다 먼저 봐야 "요청 형식 오류"로 잘못 안내하지 않는다.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        CONTENT_POLICY,
        (
            "your request was rejected",
            "safety system",
            "safety_system",
            "safety filter",
            "safety_filter",
            "content policy",
            "content_policy",
            "content_filter",
            "content moderation",
            "prohibited_content",
            "image_safety",
            "recitation",
            "blocked by",
            "안전 시스템",
            "콘텐츠 정책",
            "정책에 걸",
        ),
    ),
    (
        AUTH,
        (
            "api 키",
            "unauthorized",
            "forbidden",
            "invalid api key",
            "no auth credentials",
            "authentication",
        ),
    ),
    (
        RATE_LIMIT,
        (
            "rate limit",
            "rate-limit",
            "too many requests",
            "quota",
            "insufficient credit",
            "요청이 너무 많",
        ),
    ),
    (
        SERVER,
        (
            "internal server error",
            "bad gateway",
            "service unavailable",
            "overloaded",
            "temporarily unavailable",
        ),
    ),
    (
        NO_IMAGE,
        (
            "이미지가 정확히 한 장",
            "b64_json 이미지가 없습니다",
            "이미지 데이터가 비어",
            "이미지를 돌려주지 않았습니다",
            "no image",
        ),
    ),
    (
        NETWORK,
        (
            "연결 실패",
            "urlerror",
            "timed out",
            "timeout",
            "connection refused",
            "getaddrinfo",
            "firewall",
            "방화벽",
            "프록시",
            "certificate",
            "ssl",
            "network is unreachable",
        ),
    ),
    (
        BAD_REQUEST,
        (
            "bad request",
            "invalid request",
            "unsupported parameter",
            "지원하지 않는",
            "올바르지 않습니다",
        ),
    ),
    (
        LOCAL,
        (
            "작업자 프로세스를 시작하지 못했습니다",
            "결과를 읽지 못했습니다",
            "후처리에 실패",
            "자동 적용 실패",
            "베이크",
            "실루엣",
        ),
    ),
)


def extract_request_id(message: str) -> str:
    """제공사 문의에 필요한 ``req_…`` 요청 ID를 뽑는다. 없으면 빈 문자열."""

    match = _REQUEST_ID_PATTERN.search(str(message))
    return match.group(0) if match is not None else ""


def extract_status_code(message: str) -> int:
    """작업자가 붙인 HTTP 상태 코드를 읽는다. 없으면 0."""

    match = _STATUS_PATTERN.search(str(message))
    return int(match.group(1)) if match is not None else 0


def _kind_from_status(status: int) -> str:
    if status in (401, 403):
        return AUTH
    if status == 429:
        return RATE_LIMIT
    if 500 <= status < 600:
        return SERVER
    if 400 <= status < 500:
        return BAD_REQUEST
    return ""


def describe(kind: str, raw: str = "", request_id: str = "") -> FailureInfo:
    """분류 코드 하나를 설명·해결 방법이 붙은 ``FailureInfo``로 만든다."""

    code = str(kind) if str(kind) in _DESCRIPTIONS else UNKNOWN
    title, explanation, suggestions = _DESCRIPTIONS[code]
    text = str(raw)
    return FailureInfo(
        kind=code,
        title=title,
        explanation=explanation,
        suggestions=suggestions,
        raw=text,
        request_id=str(request_id) or extract_request_id(text),
    )


def classify_failure(message: str, fallback: str = UNKNOWN) -> FailureInfo:
    """실패 원문을 종류별로 나눈다.

    ``fallback``은 키워드와 상태 코드 어느 쪽으로도 판정되지 않을 때 쓰인다. 애드온
    내부 단계에서 난 실패는 호출부가 ``LOCAL``을 넘긴다. 이때는 내부 문구를 먼저 보는데,
    "적용 실패: OpenRouter …"처럼 조립된 문장에서 뒤쪽 키워드가 원인을 가로채면 로컬
    단계 실패가 네트워크·정책 실패로 안내되기 때문이다.
    """

    text = str(message)
    lowered = text.lower()
    groups = _KEYWORDS
    if fallback == LOCAL:
        groups = (*(pair for pair in _KEYWORDS if pair[0] == LOCAL), *_KEYWORDS)
    for kind, keywords in groups:
        if any(keyword in lowered for keyword in keywords):
            return describe(kind, text)
    status_kind = _kind_from_status(extract_status_code(text))
    if status_kind:
        return describe(status_kind, text)
    return describe(fallback, text)


def truncate_reason(text: str, limit: int = MAX_RAW_PREVIEW) -> str:
    """응답에 실린 모델 사유를 팝업에 실을 만큼만 남긴다."""

    value = " ".join(str(text).split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def raw_preview_lines(
    raw: str, width: int = RAW_LINE_WIDTH, max_lines: int = MAX_RAW_LINES
) -> tuple[str, ...]:
    """원문을 팝업 label 여러 줄로 나눈다. 넘치는 부분은 복사 버튼으로 넘긴다."""

    value = " ".join(str(raw).split())
    if not value:
        return ()
    chunks = [value[index : index + width] for index in range(0, len(value), width)]
    if len(chunks) <= max_lines:
        return tuple(chunks)
    return (*chunks[:max_lines], RAW_OVERFLOW_HINT)


def popup_lines(info: FailureInfo) -> tuple[str, ...]:
    """팝업과 대화상자가 함께 쓰는 한국어 안내 줄."""

    lines = [info.explanation, "", "해결 방법:"]
    lines.extend(f"· {suggestion}" for suggestion in info.suggestions)
    if info.request_id:
        lines.extend(("", f"요청 ID: {info.request_id}"))
    preview = raw_preview_lines(info.raw)
    if preview:
        lines.extend(("", "원문:", *preview))
    return tuple(lines)


__all__ = (
    "AUTH",
    "BAD_REQUEST",
    "CONTENT_POLICY",
    "FailureInfo",
    "LOCAL",
    "MAX_RAW_LINES",
    "MAX_RAW_PREVIEW",
    "NETWORK",
    "NO_IMAGE",
    "RATE_LIMIT",
    "RAW_LINE_WIDTH",
    "RAW_OVERFLOW_HINT",
    "SERVER",
    "UNKNOWN",
    "classify_failure",
    "describe",
    "extract_request_id",
    "extract_status_code",
    "popup_lines",
    "raw_preview_lines",
    "truncate_reason",
)
