"""Blender 없이 실행하는 생성 실패 분류 테스트.

실측 거부 문구를 그대로 고정해, 문구가 바뀌어 사용자가 다시 원인을 알 수 없게 되는
회귀를 잡는다.
"""

from __future__ import annotations

from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_errors import (
    AUTH,
    MAX_RAW_LINES,
    RAW_LINE_WIDTH,
    RAW_OVERFLOW_HINT,
    BAD_REQUEST,
    CONTENT_POLICY,
    LOCAL,
    MAX_RAW_PREVIEW,
    NETWORK,
    NO_IMAGE,
    RATE_LIMIT,
    SERVER,
    UNKNOWN,
    classify_failure,
    describe,
    raw_preview_lines,
    extract_request_id,
    extract_status_code,
    popup_lines,
    truncate_reason,
)


# 실측: OpenAI gpt-image 계열이 Spider-Man 참조 프롬프트에 돌려준 400 응답.
_OPENAI_REJECTION = (
    "OpenRouter API 오류(400): Your request was rejected as a result of our safety "
    "system. If you believe this is an error, contact us at help.openai.com and "
    "include the request ID req_8f3c21ab9d4e47f0."
)


def test_openai_safety_rejection_is_content_policy() -> None:
    info = classify_failure(_OPENAI_REJECTION)
    assert info.kind == CONTENT_POLICY
    assert info.request_id == "req_8f3c21ab9d4e47f0"
    assert info.raw == _OPENAI_REJECTION
    # 400이 섞여 있어도 요청 형식 오류로 안내하면 안 된다.
    assert extract_status_code(_OPENAI_REJECTION) == 400
    joined = " ".join(info.suggestions)
    assert "유명 캐릭터" in joined
    assert "자체 원화" in joined
    assert "모델을 바꿔도" in joined


def test_gemini_safety_phrases_are_content_policy() -> None:
    for message in (
        "OpenRouter 오류: PROHIBITED_CONTENT",
        "OpenRouter 오류: IMAGE_SAFETY",
        "generation blocked by the provider",
        "candidate finish reason: RECITATION",
        "요청이 콘텐츠 정책에 걸렸습니다",
    ):
        assert classify_failure(message).kind == CONTENT_POLICY, message


def test_status_codes_split_auth_rate_limit_and_server() -> None:
    assert classify_failure("OpenRouter API 오류(401): missing credentials").kind == AUTH
    assert classify_failure("OpenRouter API 오류(403): denied").kind == AUTH
    assert classify_failure("OpenRouter API 오류(429): slow down").kind == RATE_LIMIT
    assert classify_failure("OpenRouter API 오류(500): upstream failed").kind == SERVER
    assert classify_failure("OpenRouter API 오류(503): upstream failed").kind == SERVER
    assert classify_failure("OpenRouter API 오류(402): payment required").kind == BAD_REQUEST
    # 키워드는 상태 코드 없이도 같은 결론을 낸다.
    assert classify_failure("OpenRouter API 키가 없습니다.").kind == AUTH
    assert classify_failure("Rate limit exceeded for this model").kind == RATE_LIMIT
    assert classify_failure("Internal server error").kind == SERVER


def test_network_and_no_image_and_local_kinds() -> None:
    assert classify_failure("OpenRouter API 연결 실패: timed out").kind == NETWORK
    assert (
        classify_failure("OpenRouter Images 응답에는 이미지가 정확히 한 장 있어야 합니다.").kind
        == NO_IMAGE
    )
    assert (
        classify_failure("OpenRouter Images 응답에 b64_json 이미지가 없습니다.").kind == NO_IMAGE
    )
    assert (
        classify_failure("AI 작업자 프로세스를 시작하지 못했습니다: [Errno 13]").kind == LOCAL
    )
    assert classify_failure("3면도는 생성됐지만 자동 적용 실패: 베이크 대상 없음").kind == LOCAL
    # 분류 근거가 없으면 호출부가 준 기본값을 쓴다.
    assert classify_failure("설명할 수 없는 내부 상황", LOCAL).kind == LOCAL


def test_unknown_message_falls_back_to_unknown() -> None:
    info = classify_failure("완전히 새로운 형태의 실패 문장")
    assert info.kind == UNKNOWN
    assert info.title
    assert info.explanation
    assert info.suggestions


def test_text_only_response_message_is_content_policy_when_reason_mentions_policy() -> None:
    """이미지 없이 글로만 돌아온 거부는 사유 문장으로 정책 거부까지 좁혀진다."""

    message = (
        "OpenRouter Images 응답에는 이미지가 정확히 한 장 있어야 합니다. "
        "모델 응답: I can't create images of this character because it violates our "
        "content policy."
    )
    assert classify_failure(message).kind == CONTENT_POLICY
    # 사유가 없으면 이미지 없음 그대로 남는다.
    assert (
        classify_failure("OpenRouter Images 응답에는 이미지가 정확히 한 장 있어야 합니다.").kind
        == NO_IMAGE
    )


def test_narrow_keywords_do_not_swallow_unrelated_messages() -> None:
    """정책 거부 단어는 구 단위로만 본다. 단독 단어는 무관한 실패까지 끌어간다."""

    assert classify_failure("OpenRouter API 연결 실패: 방화벽에 blocked").kind == NETWORK
    assert classify_failure("연결이 firewall에 막혔습니다").kind == NETWORK
    assert classify_failure("프록시 설정 때문에 연결하지 못했습니다").kind == NETWORK
    # 안전 장치 자체를 가리키는 구는 그대로 정책 거부다.
    assert classify_failure("request blocked by the safety filter").kind == CONTENT_POLICY


def test_local_fallback_checks_internal_phrases_first() -> None:
    """조립된 상태 줄의 뒤쪽 키워드가 로컬 실패의 원인을 가로채면 안 된다."""

    message = "3면도는 생성됐지만 자동 적용 실패: 저장된 OpenRouter API 연결 실패 기록을 읽는 중 오류"
    assert classify_failure(message, LOCAL).kind == LOCAL
    # 기본값이 UNKNOWN이면 기존처럼 앞선 종류의 키워드가 먼저 걸린다.
    assert classify_failure(message).kind == NETWORK


def test_raw_preview_wraps_into_limited_lines() -> None:
    lines = raw_preview_lines("가" * (RAW_LINE_WIDTH * 2))
    assert lines == ("가" * RAW_LINE_WIDTH, "가" * RAW_LINE_WIDTH)
    overflow = raw_preview_lines("나" * (RAW_LINE_WIDTH * (MAX_RAW_LINES + 2)))
    assert len(overflow) == MAX_RAW_LINES + 1
    assert overflow[-1] == RAW_OVERFLOW_HINT
    assert all(len(line) <= RAW_LINE_WIDTH for line in overflow[:-1])
    assert raw_preview_lines("") == ()
    assert raw_preview_lines("  줄바꿈\n포함  ") == ("줄바꿈 포함",)


def test_finish_reason_style_refusals_are_content_policy() -> None:
    """종료 코드만 실려 온 응답도 정책 거부로 안내해야 한다."""

    base = "OpenRouter Images 응답에는 이미지가 정확히 한 장 있어야 합니다. 모델 응답: "
    for reason in ("content_filter", "PROHIBITED_CONTENT", "IMAGE_SAFETY"):
        assert classify_failure(base + reason).kind == CONTENT_POLICY, reason


def test_request_id_extraction() -> None:
    assert extract_request_id("include the request ID req_abc123.") == "req_abc123"
    assert extract_request_id("요청 ID 없음") == ""
    assert extract_status_code("네트워크 실패") == 0
    # 본문에 섞인 숫자를 상태 코드로 오해하지 않는다.
    assert extract_status_code("이미지 크기 4096 처리 실패") == 0


def test_describe_accepts_unknown_kind_and_keeps_raw() -> None:
    info = describe("", raw="원문 텍스트 req_zz9")
    assert info.kind == UNKNOWN
    assert info.raw == "원문 텍스트 req_zz9"
    assert info.request_id == "req_zz9"
    assert describe("존재하지 않는 코드").kind == UNKNOWN


def test_truncate_reason_and_popup_lines() -> None:
    long_text = "가" * (MAX_RAW_PREVIEW + 50)
    truncated = truncate_reason(long_text)
    assert len(truncated) == MAX_RAW_PREVIEW
    assert truncated.endswith("…")
    assert truncate_reason("줄바꿈\n포함  공백") == "줄바꿈 포함 공백"

    lines = popup_lines(classify_failure(_OPENAI_REJECTION))
    assert "해결 방법:" in lines
    assert any(line.startswith("· 유명 캐릭터") for line in lines)
    assert "요청 ID: req_8f3c21ab9d4e47f0" in lines
    assert all(isinstance(line, str) for line in lines)
    # 원문은 패널 폭에서 잘리지 않게 여러 줄로 나뉜다.
    assert all(len(line) <= RAW_LINE_WIDTH for line in lines)


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"실패 분류 순수 테스트 {len(tests)}/{len(tests)} 통과")
