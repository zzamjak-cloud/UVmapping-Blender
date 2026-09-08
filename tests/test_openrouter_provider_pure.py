"""OpenRouter Provider의 Blender 비의존 회귀 테스트.

두 Provider를 하나의 키·하나의 요청 형식으로 합쳤으므로, 요청 본문과 응답
해석이 OpenRouter 계약을 그대로 지키는지와 비용 계약(1회 호출·1장)이 유지되는지를
검사한다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.openrouter_provider import (
    BASE_URL,
    CHAT_COMPLETIONS_ENDPOINT,
    DEFAULT_ANALYSIS_MODEL,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_RESOLUTION,
    IMAGES_ENDPOINT,
    MAX_INPUT_REFERENCES,
    build_analysis_payload,
    build_request_headers,
    build_turnaround_payload,
    extract_analysis_text,
    extract_image_response,
    validate_model_slug,
)


_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
_JPG = base64.b64encode(b"\xff\xd8\xfffake").decode("ascii")


def _expect_error(callable_, fragment: str, kind=ValueError) -> None:
    try:
        callable_()
    except kind as exc:
        assert fragment in str(exc), f"예상 문구 {fragment!r}가 없습니다: {exc}"
        return
    raise AssertionError(f"{kind.__name__}가 발생하지 않았습니다: {fragment!r}")


def test_endpoints_point_at_openrouter_only() -> None:
    assert BASE_URL == "https://openrouter.ai/api/v1"
    assert CHAT_COMPLETIONS_ENDPOINT == "chat/completions"
    assert IMAGES_ENDPOINT == "images"
    # 기본 모델은 OpenRouter 슬러그 형식이어야 한다.
    assert DEFAULT_ANALYSIS_MODEL == "google/gemini-3.7-flash"
    assert DEFAULT_IMAGE_MODEL == "google/gemini-3-pro-image"


def test_model_slug_requires_vendor_prefix() -> None:
    assert validate_model_slug(" google/gemini-3-pro-image ") == "google/gemini-3-pro-image"
    assert validate_model_slug("openai/gpt-5.4-image-2") == "openai/gpt-5.4-image-2"
    assert validate_model_slug("google/gemini-3.7-flash:batch") == "google/gemini-3.7-flash:batch"
    for bad in ("gemini-3-pro-image", "gpt-image-2", "google/", "/model", "google//x"):
        _expect_error(lambda bad=bad: validate_model_slug(bad), "'제공자/모델' 형식")
    _expect_error(lambda: validate_model_slug(""), "OpenRouter 모델이(가) 비어 있습니다")


def test_request_headers_carry_single_bearer_key_and_attribution() -> None:
    headers = build_request_headers("  sk-or-v1-example  ")
    assert headers["Authorization"] == "Bearer sk-or-v1-example"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Title"] == "UV Mapping Blender"
    assert headers["HTTP-Referer"].startswith("https://")
    _expect_error(lambda: build_request_headers(""), "OpenRouter API 키이(가) 비어 있습니다")
    _expect_error(lambda: build_request_headers("bad\nkey"), "줄바꿈을 포함할 수 없습니다")


def test_analysis_payload_uses_openai_compatible_chat_shape() -> None:
    payload = build_analysis_payload(
        "참조를 분석하세요",
        (("image/png", _PNG), ("image/jpeg", _JPG)),
        model="google/gemini-3.7-flash",
    )
    assert payload["model"] == "google/gemini-3.7-flash"
    assert payload["response_format"] == {"type": "json_object"}
    content = payload["messages"][0]["content"]
    assert payload["messages"][0]["role"] == "user"
    assert content[0] == {"type": "text", "text": "참조를 분석하세요"}
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{_PNG}"
    assert content[2]["image_url"]["url"] == f"data:image/jpeg;base64,{_JPG}"
    # 요청 본문은 그대로 JSON 직렬화될 수 있어야 한다.
    json.dumps(payload, ensure_ascii=False)


def test_analysis_payload_rejects_invalid_inputs() -> None:
    _expect_error(
        lambda: build_analysis_payload("", (("image/png", _PNG),)),
        "분석 프롬프트이(가) 비어 있습니다",
    )
    _expect_error(lambda: build_analysis_payload("p", ()), "최소 한 장")
    _expect_error(
        lambda: build_analysis_payload("p", (("text/plain", _PNG),)),
        "안전한 image/* 값",
    )
    _expect_error(
        lambda: build_analysis_payload("p", (("image/png", "not-base64!"),)),
        "base64 데이터가 올바르지 않습니다",
    )
    _expect_error(
        lambda: build_analysis_payload("p", (("image/png", _PNG, "extra"),)),
        "(MIME 형식, base64 데이터) 쌍",
    )


def test_analysis_text_extraction_handles_string_and_part_content() -> None:
    assert extract_analysis_text(
        {"choices": [{"message": {"content": "{\"a\": 1}"}}]}
    ) == '{"a": 1}'
    assert extract_analysis_text(
        {
            "choices": [
                {"message": {"content": [{"type": "text", "text": "첫"},
                                          {"type": "text", "text": "둘"}]}}
            ]
        }
    ) == "첫\n둘"
    assert extract_analysis_text({}) == ""
    assert extract_analysis_text({"choices": []}) == ""
    _expect_error(
        lambda: extract_analysis_text({"error": {"message": "크레딧 부족"}}),
        "크레딧 부족",
        RuntimeError,
    )


def test_turnaround_payload_fixes_one_image_and_passes_references() -> None:
    payload = build_turnaround_payload(
        "3면도를 그리세요",
        (("image/png", _PNG), ("image/jpeg", _JPG)),
        model="openai/gpt-5.4-image-2",
    )
    assert payload["model"] == "openai/gpt-5.4-image-2"
    assert payload["prompt"] == "3면도를 그리세요"
    assert payload["n"] == 1
    assert payload["aspect_ratio"] == DEFAULT_ASPECT_RATIO == "21:9"
    assert payload["resolution"] == DEFAULT_RESOLUTION == "2K"
    references = payload["input_references"]
    assert len(references) == 2
    # 첫 번째는 항상 모델 형상 contact sheet다.
    assert references[0] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{_PNG}"},
    }
    assert references[1]["image_url"]["url"] == f"data:image/jpeg;base64,{_JPG}"
    json.dumps(payload, ensure_ascii=False)


def test_turnaround_payload_enforces_openrouter_reference_limit() -> None:
    too_many = tuple(("image/png", _PNG) for _ in range(MAX_INPUT_REFERENCES + 1))
    _expect_error(
        lambda: build_turnaround_payload("p", too_many),
        f"최대 {MAX_INPUT_REFERENCES}장",
    )
    _expect_error(lambda: build_turnaround_payload("p", ()), "contact sheet가 최소 한 장")
    _expect_error(lambda: build_turnaround_payload("", (("image/png", _PNG),)),
                  "3면도 프롬프트이(가) 비어 있습니다")


def test_image_response_requires_exactly_one_decodable_image() -> None:
    mime_type, data = extract_image_response(
        {"data": [{"b64_json": _PNG, "media_type": "image/png"}]}
    )
    assert mime_type == "image/png"
    assert data == base64.b64decode(_PNG)

    # media_type이 없으면 PNG로 본다.
    assert extract_image_response({"data": [{"b64_json": _PNG}]})[0] == "image/png"
    assert extract_image_response(
        {"data": [{"b64_json": _JPG, "media_type": "image/jpeg"}]}
    )[0] == "image/jpeg"

    _expect_error(
        lambda: extract_image_response({"data": [{"b64_json": _PNG}, {"b64_json": _JPG}]}),
        "정확히 한 장",
    )
    _expect_error(lambda: extract_image_response({"data": []}), "정확히 한 장")
    _expect_error(
        lambda: extract_image_response({"data": [{"b64_json": "!!!"}]}),
        "b64_json이 올바르지 않습니다",
    )
    _expect_error(
        lambda: extract_image_response(
            {"data": [{"b64_json": _PNG, "media_type": "image/gif"}]}
        ),
        "지원하지 않는 결과 이미지 형식",
    )
    _expect_error(
        lambda: extract_image_response({"error": {"message": "모델 없음"}}),
        "모델 없음",
        RuntimeError,
    )


def test_worker_routes_both_actions_through_openrouter_only() -> None:
    """작업자가 두 작업 모두 OpenRouter 엔드포인트로만 보내는지 확인한다."""

    worker = Path(__file__).resolve().parents[1] / "uvmapping" / "texture_worker.py"
    source = worker.read_text(encoding="utf-8")
    for forbidden in (
        "generativelanguage.googleapis.com",
        "api.openai.com",
        "x-goog-api-key",
        "images/edits",
        "responses",
    ):
        assert forbidden not in source, f"구 Provider 흔적이 남아 있습니다: {forbidden}"
    assert "openrouter_provider" in source
    assert '"provider"' not in source

    # 실제 프로세스로 실행해 지원하지 않는 작업이 계약대로 실패하는지 본다.
    with tempfile.TemporaryDirectory(prefix="uvmapping-openrouter-worker-") as temp_dir:
        request_path = Path(temp_dir) / "request.json"
        response_path = Path(temp_dir) / "response.json"
        request_path.write_text(
            json.dumps({"action": "unsupported", "image_paths": []}), encoding="utf-8"
        )
        completed = subprocess.run(
            (sys.executable, str(worker), "--", str(request_path), str(response_path)),
            input=b"sk-or-v1-test\n",
            capture_output=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        result = json.loads(response_path.read_text(encoding="utf-8"))
        assert result["ok"] is False
        assert "지원하지 않는 작업" in result["error"]

        # 키가 비면 네트워크 호출 전에 막혀야 한다.
        request_path.write_text(
            json.dumps({"action": "analyze", "model": DEFAULT_ANALYSIS_MODEL,
                        "prompt": "p", "image_paths": []}),
            encoding="utf-8",
        )
        completed = subprocess.run(
            (sys.executable, str(worker), "--", str(request_path), str(response_path)),
            input=b"\n",
            capture_output=True,
            timeout=60,
        )
        result = json.loads(response_path.read_text(encoding="utf-8"))
        assert result["ok"] is False
        assert "OpenRouter API 키" in result["error"]


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"OpenRouter Provider 순수 테스트 {len(tests)}/{len(tests)} 통과")
