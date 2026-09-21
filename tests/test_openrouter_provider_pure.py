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
    IMAGE_MODEL_CAPABILITIES,
    IMAGES_ENDPOINT,
    ImageModelCapability,
    MAX_INPUT_REFERENCES,
    build_analysis_payload,
    build_request_headers,
    build_turnaround_payload,
    clamp_quality,
    effective_image_size,
    extract_analysis_text,
    extract_image_response,
    model_capabilities,
    quality_for_image_size,
    supports_resolution,
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
        model=DEFAULT_IMAGE_MODEL,
    )
    assert payload["model"] == DEFAULT_IMAGE_MODEL
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


def test_turnaround_payload_rejects_aspect_ratio_outside_layout_contract() -> None:
    # 4:3은 레이아웃이 만들지 않지만 기본 모델이 받는 값이므로, 검사는 모델 목록을
    # 쓰는 등재 모델과 전역 계약을 쓰는 미등재 슬러그로 나뉜다(아래 미등재 슬러그 테스트).
    for aspect_ratio in ("", "21/9", "2:1"):
        try:
            build_turnaround_payload("3면도", (("image/png", _PNG),), aspect_ratio=aspect_ratio)
        except ValueError:
            pass
        else:
            raise AssertionError(f"레이아웃 계약 밖 종횡비를 거부해야 합니다: {aspect_ratio!r}")
    for aspect_ratio in ("1:1", "3:2", "21:9"):
        assert build_turnaround_payload("3면도", (("image/png", _PNG),), aspect_ratio=aspect_ratio)["aspect_ratio"] == aspect_ratio


def test_turnaround_payload_enforces_openrouter_reference_limit() -> None:
    # 참조 상한은 모델별 값이 먼저다. 기본 모델(Gemini)은 14장까지만 받는다.
    gemini_limit = IMAGE_MODEL_CAPABILITIES[DEFAULT_IMAGE_MODEL].max_input_references
    too_many = tuple(("image/png", _PNG) for _ in range(gemini_limit + 1))
    _expect_error(
        lambda: build_turnaround_payload("p", too_many),
        f"최대 {gemini_limit}장",
    )
    _expect_error(lambda: build_turnaround_payload("p", ()), "contact sheet가 최소 한 장")
    _expect_error(lambda: build_turnaround_payload("", (("image/png", _PNG),)),
                  "3면도 프롬프트이(가) 비어 있습니다")
    _expect_error(
        lambda: build_turnaround_payload("p", (("image/png", _PNG),), resolution="8K"),
        "1K, 2K, 4K",
    )
    grid = build_turnaround_payload("p", (("image/png", _PNG),), aspect_ratio="3:2", resolution="4K")
    assert (grid["aspect_ratio"], grid["resolution"], grid["n"]) == ("3:2", "4K", 1)


def test_turnaround_payload_accepts_two_cell_group_canvas() -> None:
    """QUAD의 2셀 그룹 캔버스(16:9)가 화이트리스트를 통과하고 비표준 비율은 막힌다."""

    payload = build_turnaround_payload(
        "좌우 2칸 캔버스를 그리세요",
        (("image/png", _PNG),),
        aspect_ratio="16:9",
        resolution="2K",
    )
    assert (payload["aspect_ratio"], payload["resolution"], payload["n"]) == ("16:9", "2K", 1)
    # 비표준 2:1은 레이아웃 계약에 없으므로 거부해야 한다.
    _expect_error(
        lambda: build_turnaround_payload("p", (("image/png", _PNG),), aspect_ratio="2:1"),
        "이미지 종횡비는",
    )

    # contact sheet 1 + FRONT 색 참조 1 + 사용자 참조 5 = 7장까지는 그대로 실린다.
    references = ((("image/png", _PNG),) * 2) + ((("image/jpeg", _JPG),) * 5)
    group = build_turnaround_payload("p", references, aspect_ratio="16:9")
    assert len(group["input_references"]) == 7 <= MAX_INPUT_REFERENCES
    assert group["n"] == 1
    json.dumps(group, ensure_ascii=False)


def test_gemini_models_send_resolution_and_no_quality() -> None:
    """resolution을 받는 모델은 예전 그대로 aspect_ratio+resolution만 보낸다."""

    for model in (
        "google/gemini-3-pro-image",
        "google/gemini-3.1-flash-image",
    ):
        payload = build_turnaround_payload(
            "p", (("image/png", _PNG),), model=model, aspect_ratio="3:2", resolution="4K"
        )
        assert payload["resolution"] == "4K"
        assert "quality" not in payload
        assert payload["n"] == 1
        assert supports_resolution(model) is True
        assert quality_for_image_size(model, "4K") is None

    # 1K 한 등급만 받는 모델은 그 밖의 크기를 거부하지 않고 1K로 낮춘다.
    lite = "google/gemini-3.1-flash-lite-image"
    for image_size in ("1K", "2K", "4K"):
        payload = build_turnaround_payload(
            "p", (("image/png", _PNG),), model=lite, resolution=image_size
        )
        assert payload["resolution"] == "1K"
        assert "quality" not in payload


def test_gpt_models_send_quality_instead_of_resolution() -> None:
    """resolution을 받지 않는 모델은 키를 빼고 quality 등급으로 크기를 전달한다."""

    for model in ("openai/gpt-5.4-image-2", "openai/gpt-image-2"):
        assert supports_resolution(model) is False
        for image_size, quality in (("1K", "medium"), ("2K", "high"), ("4K", "high")):
            payload = build_turnaround_payload(
                "p",
                (("image/png", _PNG),),
                model=model,
                aspect_ratio="21:9",
                resolution=image_size,
            )
            assert "resolution" not in payload
            assert payload["quality"] == quality
            assert payload["n"] == 1
            assert quality_for_image_size(model, image_size) == quality
            json.dumps(payload, ensure_ascii=False)


def test_gpt_image_25_tiers_use_xhigh_for_four_k() -> None:
    """2.5 계열은 xhigh를 받으므로 4K가 한 단계 위로 올라간다."""

    for model in ("openai/gpt-image-2.5-sunburst", "openai/gpt-image-2.5-flare"):
        payload = build_turnaround_payload(
            "p", (("image/png", _PNG),), model=model, aspect_ratio="16:9", resolution="4K"
        )
        assert payload["quality"] == "xhigh"
        assert "resolution" not in payload
        assert quality_for_image_size(model, "2K") == "high"
        assert quality_for_image_size(model, "1K") == "medium"


def test_explicit_quality_overrides_image_size_mapping() -> None:
    """호출자가 품질 등급을 지정하면 크기 매핑 대신 그 값을 쓴다."""

    payload = build_turnaround_payload(
        "p",
        (("image/png", _PNG),),
        model="openai/gpt-image-2.5-sunburst",
        aspect_ratio="16:9",
        resolution="1K",
        quality="max",
    )
    assert payload["quality"] == "max"
    assert "resolution" not in payload

    # 모델이 받지 않는 등급은 거부하지 않고 한 단계씩 낮춘다. xhigh는 2.5 계열에만 있다.
    assert build_turnaround_payload(
        "p", (("image/png", _PNG),), model="openai/gpt-image-2", quality="xhigh"
    )["quality"] == "high"
    assert build_turnaround_payload(
        "p", (("image/png", _PNG),), model="openai/gpt-5.4-image-2", quality="max"
    )["quality"] == "high"
    # 등급 이름 자체가 틀리면 그대로 거부한다.
    _expect_error(
        lambda: build_turnaround_payload(
            "p", (("image/png", _PNG),), model="openai/gpt-image-2", quality="ultra"
        ),
        "이미지 품질 등급은",
    )
    _expect_error(
        lambda: build_turnaround_payload(
            "p", (("image/png", _PNG),), model="openai/gpt-image-2", quality=""
        ),
        "이미지 품질 등급이(가) 비어 있습니다",
    )
    # low는 모든 GPT 계열이 받는다.
    assert build_turnaround_payload(
        "p", (("image/png", _PNG),), model="openai/gpt-5.4-image-2", quality="low"
    )["quality"] == "low"


def test_clamp_quality_walks_down_to_the_first_supported_tier() -> None:
    """요청 등급 이하의 첫 지원 등급으로 낮추고, 알 수 없는 이름만 거부한다."""

    # 2.5 계열은 사다리 전체를 받으므로 값이 그대로 남는다.
    for quality in ("max", "xhigh", "high", "medium", "low", "auto"):
        assert clamp_quality("openai/gpt-image-2.5-sunburst", quality) == quality
    # high까지만 받는 모델은 그 위 등급을 high로 낮춘다.
    for model in ("openai/gpt-image-2", "openai/gpt-5.4-image-2"):
        assert clamp_quality(model, "max") == "high"
        assert clamp_quality(model, "xhigh") == "high"
        assert clamp_quality(model, "high") == "high"
        assert clamp_quality(model, "low") == "low"
        # 앞뒤 공백은 흡수한다.
        assert clamp_quality(model, "  medium  ") == "medium"
    # quality를 쓰지 않는 모델과 미등재 슬러그는 값을 건드리지 않는다.
    assert clamp_quality(DEFAULT_IMAGE_MODEL, "xhigh") == "xhigh"
    assert clamp_quality("someone/custom-image-model", "max") == "max"
    _expect_error(lambda: clamp_quality("openai/gpt-image-2", "ultra"), "이미지 품질 등급은")
    _expect_error(lambda: clamp_quality("openai/gpt-image-2", None), "비어 있습니다")


def test_resolution_models_ignore_quality_argument() -> None:
    """resolution을 받는 모델과 미등재 슬러그에는 quality를 싣지 않는다."""

    for model in (DEFAULT_IMAGE_MODEL, "someone/custom-image-model"):
        payload = build_turnaround_payload(
            "p",
            (("image/png", _PNG),),
            model=model,
            aspect_ratio="16:9",
            resolution="2K",
            quality="max",
        )
        assert "quality" not in payload
        assert payload["resolution"] == "2K"


def test_effective_image_size_pins_gpt_models_to_one_k() -> None:
    """GPT 계열은 결과가 1K급으로 고정되므로 표시 크기도 1K로 내려간다."""

    for image_size in ("1K", "2K", "4K"):
        assert effective_image_size("openai/gpt-image-2.5-sunburst", image_size) == "1K"
        assert effective_image_size("openai/gpt-5.4-image-2", image_size) == "1K"
        # 요청한 등급을 다 받는 모델과 미등재 슬러그는 입력을 그대로 쓴다.
        assert effective_image_size(DEFAULT_IMAGE_MODEL, image_size) == image_size
        assert effective_image_size("google/gemini-3.1-flash-image", image_size) == image_size
        assert effective_image_size("someone/custom-image-model", image_size) == image_size
    _expect_error(lambda: effective_image_size(DEFAULT_IMAGE_MODEL, "8K"), "1K, 2K, 4K")
    _expect_error(lambda: effective_image_size(DEFAULT_IMAGE_MODEL, ""), "비어 있습니다")


def test_effective_image_size_walks_down_to_a_supported_tier() -> None:
    """모델이 받지 않는 해상도는 요청 이하의 첫 지원 등급으로 낮춘다."""

    lite = "google/gemini-3.1-flash-lite-image"
    for image_size in ("4K", "2K", "1K"):
        assert effective_image_size(lite, image_size) == "1K"

    # 사다리 동작을 임시 항목으로 직접 본다. 요청 이하가 없으면 지원 목록의 최솟값이다.
    probe = "example/probe-image"
    IMAGE_MODEL_CAPABILITIES[probe] = ImageModelCapability(("1:1",), ("2K", "512"), (), 4)
    try:
        assert effective_image_size(probe, "4K") == "2K"
        assert effective_image_size(probe, "2K") == "2K"
        assert effective_image_size(probe, "1K") == "512"
        high_only = "example/probe-high-only"
        IMAGE_MODEL_CAPABILITIES[high_only] = ImageModelCapability(("1:1",), ("4K",), (), 4)
        try:
            assert effective_image_size(high_only, "1K") == "4K"
        finally:
            IMAGE_MODEL_CAPABILITIES.pop(high_only)
    finally:
        IMAGE_MODEL_CAPABILITIES.pop(probe)


def test_aspect_ratio_whitelist_is_checked_per_model() -> None:
    """종횡비 검사는 모델별 목록을 쓴다. Gemini는 auto를 받지 않는다."""

    _expect_error(
        lambda: build_turnaround_payload(
            "p", (("image/png", _PNG),), model="google/gemini-3-pro-image", aspect_ratio="auto"
        ),
        "이미지 종횡비는",
    )
    # 같은 값을 GPT 계열은 받는다.
    assert build_turnaround_payload(
        "p", (("image/png", _PNG),), model="openai/gpt-image-2", aspect_ratio="auto"
    )["aspect_ratio"] == "auto"
    # QUAD·SIX·THREE가 쓰는 종횡비는 등재 모델 전부가 받는다.
    for model, capability in IMAGE_MODEL_CAPABILITIES.items():
        for aspect_ratio in ("1:1", "3:2", "16:9", "21:9"):
            assert aspect_ratio in capability.aspect_ratios, (model, aspect_ratio)


def test_unlisted_model_slug_keeps_global_contract() -> None:
    """테이블에 없는 사용자 직접 입력 슬러그는 현행 동작을 그대로 유지한다."""

    unlisted = "someone/custom-image-model"
    assert model_capabilities(unlisted) is None
    assert supports_resolution(unlisted) is True
    assert quality_for_image_size(unlisted, "4K") is None

    payload = build_turnaround_payload(
        "p", (("image/png", _PNG),), model=unlisted, aspect_ratio="16:9", resolution="2K"
    )
    assert (payload["aspect_ratio"], payload["resolution"], payload["n"]) == ("16:9", "2K", 1)
    assert "quality" not in payload
    # 전역 레이아웃 계약 밖 종횡비와 전역 참조 상한이 폴백으로 남는다.
    _expect_error(
        lambda: build_turnaround_payload(
            "p", (("image/png", _PNG),), model=unlisted, aspect_ratio="4:3"
        ),
        "이미지 종횡비는",
    )
    too_many = tuple(("image/png", _PNG) for _ in range(MAX_INPUT_REFERENCES + 1))
    _expect_error(
        lambda: build_turnaround_payload("p", too_many, model=unlisted),
        f"최대 {MAX_INPUT_REFERENCES}장",
    )


def test_model_capability_lookup_is_slug_normalized() -> None:
    """능력치 조회는 앞뒤 공백·대문자를 흡수하고 비문자열은 None을 준다."""

    assert model_capabilities("  Google/Gemini-3-Pro-Image ") is IMAGE_MODEL_CAPABILITIES[
        "google/gemini-3-pro-image"
    ]
    assert model_capabilities(None) is None
    assert model_capabilities(123) is None
    assert quality_for_image_size("openai/gpt-image-2", "8K") is None
    assert IMAGE_MODEL_CAPABILITIES["openai/gpt-image-2.5-sunburst"].max_input_references == 16
    assert IMAGE_MODEL_CAPABILITIES["google/gemini-3-pro-image"].max_input_references == 14


def test_worker_forwards_layout_aspect_ratio_and_resolution() -> None:
    """작업 JSON의 종횡비·해상도가 네트워크 호출 본문까지 그대로 전달되는지 본다."""

    from uvmapping import texture_worker

    captured: list[tuple[str, dict]] = []
    png_bytes = base64.b64decode(_PNG) + b"\x00\x00\x00\x00IEND\xaeB`\x82"

    def fake_request(endpoint, api_key, payload):
        captured.append((endpoint, payload))
        return {"data": [{"b64_json": base64.b64encode(png_bytes).decode("ascii")}]}

    original = texture_worker._openrouter_request
    texture_worker._openrouter_request = fake_request
    try:
        with tempfile.TemporaryDirectory(prefix="uvmapping-worker-layout-") as temp_dir:
            sheet = Path(temp_dir) / "sheet.png"
            sheet.write_bytes(png_bytes)
            output = Path(temp_dir) / "result.png"
            base_job = {
                "action": "turnaround",
                "model": DEFAULT_IMAGE_MODEL,
                "prompt": "p",
                "image_paths": [str(sheet)],
                "output_path": str(output),
            }
            result = texture_worker.run_job(
                {**base_job, "aspect_ratio": "3:2", "resolution": "4K"}, "sk-or-v1-test"
            )
            assert result["ok"] is True and Path(result["output_path"]).is_file()
            endpoint, payload = captured[-1]
            assert endpoint == IMAGES_ENDPOINT
            assert (payload["aspect_ratio"], payload["resolution"]) == ("3:2", "4K")
            # 값이 없으면 기존 기본값을 유지한다.
            texture_worker.run_job(base_job, "sk-or-v1-test")
            payload = captured[-1][1]
            assert (payload["aspect_ratio"], payload["resolution"]) == (
                DEFAULT_ASPECT_RATIO,
                DEFAULT_RESOLUTION,
            )
    finally:
        texture_worker._openrouter_request = original


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
