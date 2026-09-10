"""Blender 없이 실행하는 AI 손맵 텍스처 파이프라인 회귀 테스트."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_pipeline import (
    InlineImage,
    TURNAROUND_VIEWS,
    build_reference_analysis_prompt,
    build_turnaround_request,
    compile_turnaround_prompt,
    normalize_reference_analysis,
    parse_reference_analysis,
    validate_reference_image_path,
)


def test_reference_file_validation_blocks_non_images_and_symlinks() -> None:
    with tempfile.TemporaryDirectory(prefix="uvmapping-reference-security-") as temp_dir:
        root = Path(temp_dir)
        png = root / "reference.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload" + b"\x00\x00\x00\x00IEND\xaeB`\x82")
        assert validate_reference_image_path(png) == (png, "image/png")

        renamed_secret = root / "secret.png"
        renamed_secret.write_text("not an image", encoding="utf-8")
        try:
            validate_reference_image_path(renamed_secret)
        except ValueError as error:
            assert "올바른 image/png" in str(error)
        else:
            raise AssertionError("이미지로 위장한 임의 파일이 거부되어야 합니다.")

        unsupported = root / "secret.txt"
        unsupported.write_text("secret", encoding="utf-8")
        try:
            validate_reference_image_path(unsupported)
        except ValueError as error:
            assert "JPG, PNG 또는 WebP" in str(error)
        else:
            raise AssertionError("허용되지 않은 확장자가 거부되어야 합니다.")

        symlink = root / "linked.png"
        try:
            symlink.symlink_to(png)
        except OSError:
            pass
        else:
            try:
                validate_reference_image_path(symlink)
            except ValueError as error:
                assert "심볼릭 링크" in str(error)
            else:
                raise AssertionError("심볼릭 링크 참조가 거부되어야 합니다.")


def _analysis_response() -> str:
    return """모델의 분석 결과입니다.
```json
{
  "schema_version": "임의 값",
  "object_summary": "낡은 나무 상자",
  "reference_roles": [
    {"reference_index": 0, "role": "색상", "use_for": ["팔레트"], "confidence": 1.7},
  ],
  "style": {
    "art_style": "stylized hand-painted",
    "brushwork": "넓은 붓질",
    "detail_density": "중간",
    "outline_style": "없음",
    "color_palette": ["#6D4328", "황토색"],
    "shading_style": "위는 밝고 아래는 어두움",
    "edge_highlight": "모서리 밝은 선",
  },
  "surface_regions": [
    {"name": "판재", "location": "전체", "base_colors": ["갈색"],
     "material_cues": ["나뭇결"], "details": ["작은 흠집"],
     "confidence": -2, "source": "OBSERVED"},
    {"name": "뒤쪽", "location": "후면", "source": "알 수 없음"},
  ],
  "design_rules": {"preserve": ["큰 실루엣"], "exclude": ["워터마크"],},
  "uncertainty": {
    "observed": ["정면 판재"],
    "inferred": ["후면 나뭇결"],
    "conflicts": ["금속 색상"],
  },
}
```
분석을 마칩니다."""


def test_analysis_prompt_requires_one_strict_json_and_uncertainty() -> None:
    prompt = build_reference_analysis_prompt(3)

    assert "JSON 객체 하나만" in prompt
    assert '"observed"' in prompt
    assert '"inferred"' in prompt
    assert '"conflicts"' in prompt
    assert "참조 이미지 3장" in prompt


def test_analysis_prompt_rejects_empty_references() -> None:
    try:
        build_reference_analysis_prompt(0)
    except ValueError as error:
        assert "최소 한 장" in str(error)
    else:
        raise AssertionError("빈 참조 이미지 요청이 거부되어야 합니다.")


def test_parser_handles_fence_noise_trailing_comma_and_normalizes_schema() -> None:
    analysis = parse_reference_analysis(_analysis_response())

    assert analysis.schema_version == "1.0"
    assert analysis.object_summary == "낡은 나무 상자"
    assert analysis.reference_roles[0].confidence == 1.0
    assert analysis.style.color_palette == ("#6D4328", "황토색")
    assert analysis.surface_regions[0].source == "observed"
    assert analysis.surface_regions[0].confidence == 0.0
    assert analysis.surface_regions[1].source == "inferred"
    assert analysis.uncertainty.observed == ("정면 판재",)
    assert analysis.uncertainty.inferred == ("후면 나뭇결",)
    assert analysis.uncertainty.conflicts == ("금속 색상",)


def test_parser_preserves_commas_inside_strings() -> None:
    analysis = parse_reference_analysis(
        '{"object_summary":"상자,} 그대로", "style":{}, "design_rules":{},'
        ' "uncertainty":{}, "reference_roles":[], "surface_regions":[],}'
    )

    assert analysis.object_summary == "상자,} 그대로"


def test_parser_rejects_missing_object() -> None:
    try:
        parse_reference_analysis("분석 실패")
    except ValueError as error:
        assert "JSON 객체" in str(error)
    else:
        raise AssertionError("JSON 객체가 없는 응답이 거부되어야 합니다.")


def test_turnaround_request_fixes_one_call_one_image_and_view_order() -> None:
    analysis = parse_reference_analysis(_analysis_response())
    request = build_turnaround_request(
        InlineImage("image/png", "geometry-data", "geometry_contact_sheet", "model"),
        [InlineImage("image/jpeg", "reference-data", "reference", "pinterest")],
        analysis,
        "푸른색 천 장식을 추가",
    )

    assert request.views == TURNAROUND_VIEWS == ("FRONT", "RIGHT", "BACK")
    assert request.provider_call_count == 1
    assert request.output_image_count == 1
    assert request.model == "google/gemini-3-pro-image"
    assert request.aspect_ratio == "21:9"
    assert request.image_size == "2K"
    assert "한 번" in request.prompt
    assert "정확히 한 장" in request.prompt
    assert "FRONT | RIGHT SIDE | BACK" in request.prompt
    assert "같은 너비의 3열" in request.prompt
    assert "중앙 정사각형 viewport" in request.prompt
    # contact sheet와 같은 레이아웃을 강제해야 베이크 투영 좌표가 어긋나지 않는다.
    assert "그 레이아웃을 그대로 따릅니다" in request.prompt
    assert "부위 경계" in request.prompt
    assert "텍스트, 라벨, 구분선" in request.prompt
    assert "푸른색 천 장식을 추가" in request.prompt
    assert "낡은 나무 상자" in request.prompt


def test_turnaround_request_allows_prompt_only_without_references() -> None:
    request = build_turnaround_request(
        InlineImage("image/png", "geometry-data", "geometry_contact_sheet", "model"),
        [],
        None,
        "광택 있는 빨간 주사위, 흰색 눈금",
    )

    assert request.reference_images == ()
    assert "광택 있는 빨간 주사위" in request.prompt
    assert "참조 이미지가 없습니다" in request.prompt
    assert "분석 JSON:" not in request.prompt
    assert "FRONT | RIGHT SIDE | BACK" in request.prompt


def test_prompt_only_turnaround_requires_user_instruction() -> None:
    try:
        build_turnaround_request(
            InlineImage("image/png", "geometry-data", "geometry_contact_sheet"),
            [],
            None,
            "   ",
        )
    except ValueError as error:
        assert "사용자 지시가 필요" in str(error)
    else:
        raise AssertionError("빈 프롬프트의 참조 없는 생성이 거부되어야 합니다.")


def test_turnaround_request_rejects_cost_contract_changes() -> None:
    request = build_turnaround_request(
        InlineImage("image/png", "geometry-data", "geometry_contact_sheet"),
        [InlineImage("image/png", "reference-data")],
        parse_reference_analysis('{"object_summary":"상자"}'),
    )

    for changed in (
        {"provider_call_count": 3},
        {"output_image_count": 3},
        {"views": ("FRONT", "LEFT", "BACK")},
    ):
        try:
            replace(request, **changed)
        except ValueError:
            pass
        else:
            raise AssertionError("고정된 비용 또는 시점 계약의 변경이 거부되어야 합니다.")


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
    print(f"순수 AI 텍스처 파이프라인 테스트 {len(tests)}/{len(tests)} 통과")


def test_turnaround_prompt_keeps_geometry_authoritative_without_reference_images() -> None:
    analysis = normalize_reference_analysis(
        {"object_summary": "낡은 나무 상자", "style": {"palette": ["#8B5A2B"]}}
    )
    prompt = compile_turnaround_prompt(analysis, "", reference_image_count=0)
    assert "입력 이미지는 첫 번째 한 장뿐입니다" in prompt
    assert "분석 JSON 텍스트만 근거로" in prompt
    assert "형상 계약(다른 모든 지시보다 우선)" in prompt
    assert "한 픽셀도 재해석하지 않습니다" in prompt
    # 자세 부위를 열거하면 오히려 이미지 모델이 손동작을 만들어 낸다.
    assert "팔" not in prompt.split("형상 계약")[1].split("분석 JSON")[0]
    assert "흰 배경 위의 회색 3D 모델" in prompt
    # 분석 기록용 필드는 생성 프롬프트로 새지 않는다.
    assert "uncertainty" not in prompt
    assert "reference_roles" not in prompt


def test_turnaround_prompt_limits_reference_images_to_palette() -> None:
    analysis = normalize_reference_analysis({"object_summary": "소방관 캐릭터"})
    prompt = compile_turnaround_prompt(analysis, "", reference_image_count=2)
    assert "두 번째 이후 이미지 2장(role=palette_only)" in prompt
    assert "실루엣, 부품 구성은 절대 가져오지 않습니다" in prompt


def test_turnaround_prompt_rejects_reference_images_without_analysis() -> None:
    try:
        compile_turnaround_prompt(None, "지시", reference_image_count=1)
    except ValueError as error:
        assert "참조 분석" in str(error)
    else:
        raise AssertionError("분석 없이 참조 이미지를 보내면 거부해야 합니다.")


def test_analysis_prompt_excludes_pose_and_composition() -> None:
    prompt = build_reference_analysis_prompt(1)
    # 참조의 자세가 분석 텍스트를 타고 생성 형상으로 새면 안 된다.
    assert "자세, 포즈, 손동작" in prompt
    assert "기록하지 않습니다" in prompt


if __name__ == "__main__":
    main()
