"""Blender 없이 실행하는 OpenAI Provider 순수 함수 회귀 테스트."""

from __future__ import annotations

import base64
from pathlib import Path
import sys
import tempfile


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.openai_provider import (
    build_openai_analysis_payload,
    build_openai_image_edit_multipart,
    extract_openai_image_response,
    extract_openai_response_text,
    validate_gpt_image_2_size,
)
from uvmapping import texture_worker


def _encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _assert_value_error(function, *args, contains: str = "", **kwargs) -> None:
    try:
        function(*args, **kwargs)
    except ValueError as error:
        if contains:
            assert contains in str(error)
    else:
        raise AssertionError("잘못된 입력이 ValueError로 거부되어야 합니다.")


def test_analysis_payload_contains_prompt_and_all_data_images() -> None:
    payload = build_openai_analysis_payload(
        "두 참조의 공통 스타일을 JSON으로 분석",
        [("image/png", _encoded(b"png")), ("image/jpeg", _encoded(b"jpeg"))],
    )

    assert payload["model"] == "gpt-5.6"
    content = payload["input"][0]["content"]
    assert content[0] == {
        "type": "input_text",
        "text": "두 참조의 공통 스타일을 JSON으로 분석",
    }
    assert [part["image_url"] for part in content[1:]] == [
        f"data:image/png;base64,{_encoded(b'png')}",
        f"data:image/jpeg;base64,{_encoded(b'jpeg')}",
    ]
    assert all(part["type"] == "input_image" for part in content[1:])


def test_analysis_payload_rejects_empty_and_invalid_images() -> None:
    _assert_value_error(build_openai_analysis_payload, "분석", [], contains="최소 한 장")
    _assert_value_error(
        build_openai_analysis_payload,
        "분석",
        [("image/png", "not base64")],
        contains="base64",
    )
    _assert_value_error(
        build_openai_analysis_payload,
        "분석",
        [("text/plain", _encoded(b"data"))],
        contains="image/*",
    )


def test_response_text_extracts_only_nested_output_text_in_order() -> None:
    response = {
        "output_text": "사용하지 않는 최상위 값",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "첫 번째"},
                    {"type": "refusal", "refusal": "무시"},
                ],
            },
            {"content": [{"type": "output_text", "text": "두 번째"}]},
            {"content": "잘못된 형식"},
        ],
    }

    assert extract_openai_response_text(response) == "첫 번째\n두 번째"
    assert extract_openai_response_text({}) == ""


def test_gpt_image_2_size_accepts_exact_twenty_one_by_nine_output() -> None:
    assert validate_gpt_image_2_size("2352x1008") == (2352, 1008)
    assert 2352 / 1008 == 21 / 9
    assert 2352 // 3 == 784


def test_gpt_image_2_size_rejects_each_official_constraint() -> None:
    invalid_cases = [
        ("2300x1008", "16의 배수"),
        ("3856x1024", "3840"),
        ("3072x1008", "3:1"),
        ("800x800", "총 픽셀"),
        ("3840x2176", "총 픽셀"),
        ("0x1024", "0보다 커야"),
        ("1024", "WIDTHxHEIGHT"),
    ]
    for size, message in invalid_cases:
        _assert_value_error(validate_gpt_image_2_size, size, contains=message)


def test_multipart_preserves_fields_files_and_exact_crlf() -> None:
    boundary = "UVMappingDeterministicBoundary"
    content_type, body = build_openai_image_edit_multipart(
        "FRONT | RIGHT | BACK 한 장",
        [
            ("geometry.png", "image/png", b"geometry-bytes"),
            ("핀터레스트.jpg", "image/jpeg", b"reference-bytes"),
        ],
        boundary=boundary,
        background="auto",
    )

    assert content_type == f"multipart/form-data; boundary={boundary}"
    assert body.startswith(f"--{boundary}\r\n".encode("ascii"))
    assert body.endswith(f"--{boundary}--\r\n".encode("ascii"))
    assert b"\n" not in body.replace(b"\r\n", b"")
    assert b'form-data; name="model"\r\n\r\ngpt-image-2\r\n' in body
    assert b'form-data; name="n"\r\n\r\n1\r\n' in body
    assert b'form-data; name="size"\r\n\r\n2352x1008\r\n' in body
    assert b'form-data; name="quality"\r\n\r\nhigh\r\n' in body
    assert b'form-data; name="output_format"\r\n\r\npng\r\n' in body
    assert b'form-data; name="background"\r\n\r\nauto\r\n' in body
    assert body.count(b'form-data; name="image[]"') == 2
    assert b'filename="geometry.png"\r\nContent-Type: image/png' in body
    assert 'filename="핀터레스트.jpg"\r\nContent-Type: image/jpeg'.encode("utf-8") in body
    assert b"geometry-bytes\r\n" in body
    assert b"reference-bytes\r\n" in body


def test_multipart_default_boundary_is_deterministic() -> None:
    arguments = ("프롬프트", [("a.png", "image/png", b"same")])
    first = build_openai_image_edit_multipart(*arguments)
    second = build_openai_image_edit_multipart(*arguments)

    assert first == second
    assert first[0].startswith("multipart/form-data; boundary=----UVMappingOpenAI")


def test_multipart_rejects_invalid_contract_changes() -> None:
    image = [("a.png", "image/png", b"image")]
    _assert_value_error(build_openai_image_edit_multipart, "생성", [], contains="최소 한 장")
    _assert_value_error(
        build_openai_image_edit_multipart,
        "생성",
        image,
        size="2300x1024",
        contains="16의 배수",
    )
    _assert_value_error(
        build_openai_image_edit_multipart,
        "생성",
        image,
        background="transparent",
        contains="opaque 또는 auto",
    )
    _assert_value_error(
        build_openai_image_edit_multipart,
        "생성",
        image,
        boundary="잘못된경계",
        contains="ASCII",
    )
    _assert_value_error(
        build_openai_image_edit_multipart,
        "생성\r\n--fixed",
        image,
        boundary="fixed",
        contains="충돌",
    )


def test_image_response_requires_exactly_one_strict_base64_image() -> None:
    image_bytes = b"\x89PNG\r\n\x1a\ncontent"
    mime_type, decoded = extract_openai_image_response(
        {"data": [{"b64_json": _encoded(image_bytes)}]}
    )

    assert mime_type == "image/png"
    assert decoded == image_bytes
    assert extract_openai_image_response(
        {"data": [{"b64_json": _encoded(b"jpeg")}]},
        output_format="jpeg",
    ) == ("image/jpeg", b"jpeg")

    for response in (
        {},
        {"data": []},
        {"data": [{"b64_json": _encoded(b"a")}, {"b64_json": _encoded(b"b")}]},
        {"data": [{}]},
        {"data": [{"b64_json": "%%%"}]},
    ):
        _assert_value_error(extract_openai_image_response, response)


def test_worker_routes_openai_analysis_and_turnaround_once_each() -> None:
    original_request = texture_worker._openai_request
    calls = []

    def fake_request(endpoint, api_key, body, content_type):
        calls.append((endpoint, api_key, body, content_type))
        if endpoint == "responses":
            return {
                "output": [
                    {"content": [{"type": "output_text", "text": '{"ok":true}'}]}
                ]
            }
        return {"data": [{"b64_json": _encoded(b"generated-png")}]}

    texture_worker._openai_request = fake_request
    try:
        with tempfile.TemporaryDirectory(prefix="uvmapping-openai-worker-") as temp_dir:
            root = Path(temp_dir)
            geometry = root / "geometry.png"
            reference = root / "reference.jpg"
            geometry.write_bytes(
                b"\x89PNG\r\n\x1a\ngeometry\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            reference.write_bytes(b"\xff\xd8\xffreference\xff\xd9")

            analysis = texture_worker.run_job(
                {
                    "action": "analyze",
                    "provider": "OPENAI",
                    "model": "gpt-5.6",
                    "prompt": "분석",
                    "image_paths": [str(reference)],
                },
                "analysis-key",
            )
            assert analysis == {"ok": True, "text": '{"ok":true}'}

            result = texture_worker.run_job(
                {
                    "action": "turnaround",
                    "provider": "OPENAI",
                    "model": "gpt-image-2",
                    "prompt": "첫 이미지는 형상, 두 번째는 스타일 참조",
                    "image_paths": [str(geometry), str(reference)],
                    "output_path": str(root / "turnaround.png"),
                },
                "image-key",
            )
            assert Path(result["output_path"]).read_bytes() == b"generated-png"
    finally:
        texture_worker._openai_request = original_request

    assert [call[0] for call in calls] == ["responses", "images/edits"]
    assert calls[0][1] == "analysis-key" and calls[1][1] == "image-key"
    assert calls[1][2].count(b'form-data; name="image[]"') == 2


def main() -> None:
    tests = [
        value
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    ]
    tests.sort(key=lambda test: test.__name__)
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OpenAI Provider 순수 테스트 {len(tests)}/{len(tests)} 통과")


if __name__ == "__main__":
    main()
