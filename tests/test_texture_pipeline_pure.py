"""Blender 없이 실행하는 AI 손맵 텍스처 파이프라인 회귀 테스트."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_pipeline import (
    IMAGE_QUALITY_OPTIONS,
    ALL_VIEWS,
    ASPECT_RATIO_OPTIONS,
    DEFAULT_LAYOUT_NAME,
    FRONT_COLOR_REFERENCE_ROLE,
    GROUP_LAYOUTS,
    IMAGE_SIZE_OPTIONS,
    INLINE_IMAGE_ROLES,
    MAX_TURNAROUND_GROUPS,
    resolve_image_size,
    InlineImage,
    LEGACY_LAYOUT_NAME,
    TURNAROUND_COMPOSITIONS,
    TURNAROUND_LAYOUTS,
    TURNAROUND_VIEWS,
    TurnaroundBatchRequest,
    TurnaroundComposition,
    TurnaroundLayout,
    aspect_value,
    build_reference_analysis_prompt,
    build_turnaround_batch_request,
    build_turnaround_request,
    compile_sequential_view_prompt,
    compile_turnaround_prompt,
    normalize_reference_analysis,
    parse_reference_analysis,
    resolve_composition,
    resolve_layout,
    single_view_layout,
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


def _expect_value_error(callable_, fragment: str) -> None:
    try:
        callable_()
    except ValueError as error:
        assert fragment in str(error), f"예상 문구 {fragment!r}가 없습니다: {error}"
        return
    raise AssertionError(f"ValueError가 발생하지 않았습니다: {fragment!r}")


def test_layouts_define_square_cells_from_top_row() -> None:
    three = resolve_layout("THREE")
    six = resolve_layout("SIX")
    assert resolve_layout(None) is three
    assert DEFAULT_LAYOUT_NAME == "SIX"
    assert three.views == TURNAROUND_VIEWS == ("FRONT", "RIGHT", "BACK")
    assert (three.columns, three.rows, three.aspect_ratio) == (3, 1, "21:9")
    assert six.views == ALL_VIEWS == ("FRONT", "RIGHT", "BACK", "LEFT", "TOP", "BOTTOM")
    assert (six.columns, six.rows, six.aspect_ratio) == (3, 2, "3:2")
    # 좌하단 원점 좌표에서 첫 행(FRONT/RIGHT/BACK)이 위쪽 절반이어야 한다.
    assert six.grid_cell_bounds(300, 200, 0) == (0, 100, 100, 200)
    assert six.grid_cell_bounds(300, 200, 2) == (200, 100, 300, 200)
    assert six.grid_cell_bounds(300, 200, 3) == (0, 0, 100, 100)
    assert six.grid_cell_bounds(300, 200, 5) == (200, 0, 300, 100)
    assert six.cell_index("top") == 4
    assert three.grid_cell_bounds(336, 144, 1) == (112, 0, 224, 144)
    single = single_view_layout("bottom")
    assert (single.name, single.views, single.aspect_ratio) == ("SINGLE_VIEW", ("BOTTOM",), "1:1")
    assert single.grid_cell_bounds(64, 64, 0) == (0, 0, 64, 64)
    _expect_value_error(lambda: resolve_layout("NINE"), "지원하지 않는 3면도 레이아웃")
    _expect_value_error(lambda: resolve_layout("SINGLE_VIEW"), "single_view_layout")
    _expect_value_error(lambda: single_view_layout("DIAGONAL"), "지원하지 않는 시점")
    _expect_value_error(
        lambda: TurnaroundLayout("BAD", ("FRONT", "BACK"), 3, 1, "21:9"), "셀 수와 시점 수"
    )
    _expect_value_error(lambda: six.grid_cell_bounds(300, 200, 6), "범위")


def test_six_view_prompt_describes_grid_rows_and_top_bottom_orientation() -> None:
    analysis = normalize_reference_analysis({"object_summary": "낡은 나무 상자"})
    prompt = compile_turnaround_prompt(analysis, "", layout="SIX")
    assert "6시점도 한 장" in prompt
    assert "3:2 캔버스를 같은 너비의 3열과 같은 높이의 2행, 총 6칸" in prompt
    assert "윗줄은 왼쪽부터 FRONT | RIGHT SIDE | BACK / 아랫줄은 LEFT SIDE | TOP | BOTTOM 순서" in prompt
    assert "3:2 3열 2행 배치이므로 그 레이아웃을 그대로 따릅니다" in prompt
    assert "정면(FRONT)이 아래쪽, 오른쪽(RIGHT)이 오른쪽에 옵니다" in prompt
    assert "정면(FRONT)이 위쪽, 오른쪽(RIGHT)이 오른쪽에 옵니다" in prompt
    assert "실제 왼쪽 면" in prompt
    # 형상 계약은 레이아웃과 무관하게 paint-over 지시로 시작한다.
    contract = prompt.split("형상 계약(다른 모든 지시보다 우선):")[1].split("분석 JSON")[0]
    assert contract.lstrip().startswith("- 이 작업은 새 그림을 그리는 것이 아니라")
    assert "픽셀 단위로 정렬된 채색(paint-over)" in contract
    assert "실루엣 변경 금지" in contract
    assert "팔" not in contract
    # 3열 프롬프트에는 6시점 문구가 섞이지 않는다.
    three = compile_turnaround_prompt(analysis, "", layout="THREE")
    assert "3면도 한 장" in three
    assert "2행" not in three
    assert "TOP" not in three
    assert three == compile_turnaround_prompt(analysis, "")


def test_contact_sheet_cells_and_crop_cells_share_one_rule() -> None:
    """캔버스 여백이 생기는 21:9와 여백 없는 3:2 모두 합성 셀과 크롭 셀이 같은 위치여야 한다."""

    for name, capture in (("THREE", 1024), ("SIX", 1024), ("THREE", 112)):
        layout = TURNAROUND_LAYOUTS[name]
        width, height = layout.canvas_size(capture)
        assert abs(width / height - aspect_value(layout.aspect_ratio)) < 0.01, (name, width, height)
        for index in range(layout.cell_count):
            left, bottom, right, top = layout.grid_cell_bounds(width, height, index)
            x, y = layout.centered_cell_origin(width, height, index, capture, capture)
            # 캡처는 셀 안에 가운데 놓이고, 셀은 캔버스를 균등 분할한다.
            assert left <= x and x + capture <= right, (name, index, left, x, right)
            assert bottom <= y and y + capture <= top, (name, index, bottom, y, top)
            assert (x - left) == (right - (x + capture)) or (x - left) + 1 == (right - (x + capture))
            assert right - left == width // layout.columns and top - bottom == height // layout.rows
        # 첫 셀은 항상 캔버스 위쪽 행이다.
        assert layout.grid_cell_bounds(width, height, 0)[3] == height - (height % layout.rows)
    assert ASPECT_RATIO_OPTIONS == ("16:9", "1:1", "21:9", "3:2")
    assert resolve_layout(None).name == LEGACY_LAYOUT_NAME == "THREE"
    assert DEFAULT_LAYOUT_NAME == "SIX"
    try:
        TURNAROUND_LAYOUTS["SIX"].centered_cell_origin(300, 200, 0, 150, 150)
    except ValueError:
        pass
    else:
        raise AssertionError("셀보다 큰 내용은 거부해야 합니다.")


def test_turnaround_request_follows_layout_contract() -> None:
    sheet = InlineImage("image/png", "geometry-data", "geometry_contact_sheet", "model")
    analysis = parse_reference_analysis('{"object_summary":"상자"}')
    six = build_turnaround_request(sheet, [], analysis, "", layout_name="SIX", image_size="4K")
    assert six.layout_name == "SIX"
    assert six.views == ALL_VIEWS
    assert six.aspect_ratio == "3:2"
    assert six.image_size == "4K"
    assert six.provider_call_count == 1 and six.output_image_count == 1
    three = build_turnaround_request(sheet, [], analysis, "", layout_name="THREE")
    assert (three.layout_name, three.aspect_ratio, three.views) == ("THREE", "21:9", TURNAROUND_VIEWS)
    # 레이아웃과 어긋나는 종횡비·시점·해상도는 거부한다.
    _expect_value_error(lambda: replace(six, aspect_ratio="21:9"), "종횡비는 3:2")
    _expect_value_error(lambda: replace(three, aspect_ratio="3:2"), "종횡비는 21:9")
    _expect_value_error(lambda: replace(six, views=TURNAROUND_VIEWS), "시점 순서")
    _expect_value_error(lambda: replace(six, image_size="8K"), ", ".join(IMAGE_SIZE_OPTIONS))
    _expect_value_error(
        lambda: build_turnaround_request(sheet, [], analysis, "", layout_name="NINE"),
        "지원하지 않는 3면도 레이아웃",
    )
    _expect_value_error(
        lambda: build_turnaround_request(sheet, [], analysis, "", layout_name="SINGLE_VIEW"),
        "시점(view)이 필요",
    )
    assert set(TURNAROUND_LAYOUTS) == {"THREE", "SIX"}


def test_request_carries_optional_quality_grade() -> None:
    sheet = InlineImage("image/png", "geometry-data", "geometry_contact_sheet", "model")
    analysis = parse_reference_analysis('{"object_summary":"상자"}')
    # 기본은 등급 없음: 크기를 받는 모델이 대부분이라 Provider가 알아서 정한다.
    default = build_turnaround_request(sheet, [], analysis, "", layout_name="SIX")
    assert default.quality is None
    graded = build_turnaround_request(
        sheet, [], analysis, "", layout_name="SIX", quality="xhigh"
    )
    assert graded.quality == "xhigh"
    for grade in IMAGE_QUALITY_OPTIONS:
        assert replace(graded, quality=grade).quality == grade
    _expect_value_error(lambda: replace(graded, quality="ultra"), ", ".join(IMAGE_QUALITY_OPTIONS))
    _expect_value_error(lambda: replace(graded, quality="XHIGH"), ", ".join(IMAGE_QUALITY_OPTIONS))


def test_batch_request_applies_one_quality_grade_to_every_group() -> None:
    sheets = {
        group.name: InlineImage("image/png", f"{group.name}-data", "geometry_contact_sheet", group.name)
        for group in resolve_composition("QUAD").groups
    }
    analysis = parse_reference_analysis('{"object_summary":"상자"}')
    batch = build_turnaround_batch_request(
        "QUAD", sheets, (), analysis, "", image_size="1K", quality="high"
    )
    assert all(request.quality == "high" for request in batch.requests)
    plain = build_turnaround_batch_request("QUAD", sheets, (), analysis, "", image_size="1K")
    assert all(request.quality is None for request in plain.requests)


def test_sequential_view_prompt_paints_only_unpainted_regions() -> None:
    sheet = InlineImage("image/png", "geometry-data", "geometry_contact_sheet", "model")
    analysis = normalize_reference_analysis({"object_summary": "낡은 나무 상자"})
    first = compile_sequential_view_prompt(analysis, "", view="front", painted_views=())
    assert "단일 시점 채색 한 장" in first
    assert "이 시점(FRONT)의 회색 실루엣 전체를 채색하세요" in first
    assert "흰 배경 위의 회색 3D 모델을 FRONT에서 본 모습" in first
    assert "하나의 1:1 캔버스에 시점 하나만 담습니다" in first
    assert "정확히 한 장" in first
    assert "픽셀 단위로 정렬된 채색(paint-over)" in first
    assert "텍스트, 라벨, 구분선" in first
    assert "회색(미채색) 영역만" not in first

    later = compile_sequential_view_prompt(
        analysis, "푸른 천", view="TOP", painted_views=("FRONT", "RIGHT")
    )
    assert "이 시점(TOP)에서 회색으로 남은 미채색 영역만 채색하세요" in later
    assert "이미 일부가 채색된 모델을 TOP에서 본 모습" in later
    assert "FRONT, RIGHT SIDE 시점에서 확정된 텍스처이므로 한 픽셀도 바꾸지 않고" in later
    assert "회색(미채색) 영역만 인접한 채색 영역과 색·무늬·명암이 이어지도록" in later
    assert "정면(FRONT)이 아래쪽" in later
    assert "푸른 천" in later

    request = build_turnaround_request(
        sheet, [], analysis, "", layout_name="SINGLE_VIEW", view="right", painted_views=("FRONT",)
    )
    assert (request.layout_name, request.views, request.aspect_ratio) == ("SINGLE_VIEW", ("RIGHT",), "1:1")
    assert "FRONT 시점에서 확정된 텍스처" in request.prompt
    _expect_value_error(lambda: replace(request, views=("RIGHT", "LEFT")), "시점 하나만")
    _expect_value_error(
        lambda: compile_sequential_view_prompt(None, "지시", view="front", reference_image_count=1),
        "참조 분석",
    )


def test_image_size_defaults_follow_layout_and_auto() -> None:
    # 비용 절충: 6면도 2K, 3면도 1K, 순차 모드 시점당 1K. 4K는 명시 선택만.
    assert resolve_image_size("SIX", "AUTO") == "2K"
    assert resolve_image_size("THREE", None) == "1K"
    assert resolve_image_size("SIX", "") == "2K"
    assert resolve_image_size("SIX", "4k") == "4K"
    assert resolve_image_size(TURNAROUND_LAYOUTS["THREE"], "2K") == "2K"
    from uvmapping.texture_pipeline import single_view_layout

    assert resolve_image_size(single_view_layout("FRONT"), "AUTO") == "1K"
    _expect_value_error(lambda: resolve_image_size("SIX", "8K"), ", ".join(IMAGE_SIZE_OPTIONS))


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


def test_regeneration_feedback_adds_mask_contract_outside_shape_contract() -> None:
    analysis = normalize_reference_analysis({"object_summary": "병사 캐릭터"})
    plain = compile_turnaround_prompt(analysis, "", layout="SIX")
    assert "이전 시도 교정" not in plain
    prompt = compile_turnaround_prompt(
        analysis, "", layout="SIX", regeneration_feedback=("FRONT", "back")
    )
    assert "이전 시도 교정:" in prompt
    assert "FRONT, BACK 시점의 실루엣 내부 구조가 가이드와 달랐습니다" in prompt
    assert "배경이 보이는 모든 틈" in prompt
    assert "회색 영역은 빠짐없이 채색" in prompt
    # 교정 문단은 형상 계약 구간 밖에 있고, 자세 부위(팔)를 열거하지 않는다.
    shape_section = prompt.split("형상 계약")[1].split("이전 시도 교정")[0]
    assert "팔" not in shape_section
    feedback_section = prompt.split("이전 시도 교정")[1].split("분석 JSON")[0]
    assert "팔" not in feedback_section
    request = build_turnaround_request(
        InlineImage("image/png", "geometry-data", "geometry_contact_sheet"),
        [],
        analysis,
        "",
        layout_name="SIX",
        regeneration_feedback=("LEFT",),
    )
    assert "LEFT SIDE 시점" in request.prompt


def _quad_contact_sheets() -> dict[str, InlineImage]:
    return {
        name: InlineImage("image/png", f"{name}-guide", "geometry_contact_sheet", name)
        for name in resolve_composition("QUAD").group_names
    }


def test_quad_composition_covers_every_view_exactly_once_in_two_rounds() -> None:
    quad = resolve_composition("QUAD")

    assert quad.name == "QUAD"
    assert len(quad.groups) == MAX_TURNAROUND_GROUPS == 4
    assert quad.group_names == ("QUAD_FRONT", "QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS")
    # 시점 합집합은 6면 전체이고 그룹 간 중복이 없다(순서는 그룹 순서를 따른다).
    assert set(quad.views) == set(ALL_VIEWS)
    assert len(quad.views) == len(set(quad.views)) == len(ALL_VIEWS)
    assert quad.views == ("FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM")
    # 라운드1은 FRONT 단독, 라운드2는 나머지 3그룹 병렬.
    assert quad.rounds == (("QUAD_FRONT",), ("QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS"))
    assert sorted(name for names in quad.rounds for name in names) == sorted(quad.group_names)
    assert quad.round_index("QUAD_FRONT") == 0
    assert quad.round_index("QUAD_CAPS") == 1
    assert quad.group_of_view("left").name == "QUAD_SIDES"
    assert quad.group_of_view("BOTTOM").name == "QUAD_CAPS"
    assert quad.default_image_size == "2K"
    assert quad.is_single_canvas is False

    # THREE/SIX는 그룹 1개짜리 구성으로 감싼 것이며 기존 레이아웃을 그대로 쓴다.
    for name in ("THREE", "SIX"):
        composition = resolve_composition(name)
        assert composition.groups == (TURNAROUND_LAYOUTS[name],)
        assert composition.rounds == ((name,),)
        assert composition.views == TURNAROUND_LAYOUTS[name].views
        assert composition.is_single_canvas is True
    assert resolve_composition(None).name == LEGACY_LAYOUT_NAME == "THREE"
    assert resolve_composition("") .name == "THREE"
    assert resolve_composition(resolve_composition("SIX")).name == "SIX"
    assert set(TURNAROUND_COMPOSITIONS) == {"THREE", "SIX", "QUAD"}
    _expect_value_error(lambda: resolve_composition("NINE"), "지원하지 않는 다면도 구성")
    _expect_value_error(lambda: quad.group("QUAD_NOSE"), "구성에 없는 그룹")
    _expect_value_error(lambda: quad.group_of_view("DIAGONAL"), "구성에 없는 시점")


def test_composition_rejects_broken_invariants() -> None:
    front = GROUP_LAYOUTS["QUAD_FRONT"]
    sides = GROUP_LAYOUTS["QUAD_SIDES"]
    caps = GROUP_LAYOUTS["QUAD_CAPS"]
    back = GROUP_LAYOUTS["QUAD_BACK"]
    overlapping = TurnaroundLayout("OVERLAP", ("LEFT", "TOP"), 2, 1, "16:9")

    _expect_value_error(
        lambda: TurnaroundComposition("TOO_MANY", (front, back, sides, caps, overlapping), ()),
        f"{MAX_TURNAROUND_GROUPS}개 이하",
    )
    _expect_value_error(lambda: TurnaroundComposition("EMPTY", (), ()), "1개 이상")
    _expect_value_error(
        lambda: TurnaroundComposition("DUP", (front, front), (("QUAD_FRONT",), ("QUAD_FRONT",))),
        "그룹 이름은 중복될 수 없습니다",
    )
    _expect_value_error(
        lambda: TurnaroundComposition("VIEWDUP", (sides, overlapping), (("QUAD_SIDES", "OVERLAP"),)),
        "그룹 간에도 중복될 수 없습니다",
    )
    _expect_value_error(
        lambda: TurnaroundComposition("MISSING", (front, back), (("QUAD_FRONT",),)),
        "정확히 한 번씩",
    )


def test_quad_group_cells_share_one_rule_on_wide_and_square_canvases() -> None:
    """2×1 16:9와 1×1 1:1에서도 합성 원점과 크롭 경계가 같은 셀 규칙을 공유해야 한다."""

    sides = GROUP_LAYOUTS["QUAD_SIDES"]
    assert (sides.views, sides.columns, sides.rows, sides.aspect_ratio) == (
        ("LEFT", "RIGHT"),
        2,
        1,
        "16:9",
    )
    assert GROUP_LAYOUTS["QUAD_CAPS"].views == ("TOP", "BOTTOM")
    assert GROUP_LAYOUTS["QUAD_FRONT"].views == ("FRONT",)
    assert GROUP_LAYOUTS["QUAD_BACK"].views == ("BACK",)
    # 계획서 결정 1의 배치 산술: 캡처 1024의 2셀 16:9 캔버스는 2048×1152다.
    assert sides.canvas_size(1024) == (2048, 1152)
    assert GROUP_LAYOUTS["QUAD_FRONT"].canvas_size(1024) == (1024, 1024)
    assert sides.centered_cell_origin(2048, 1152, 0, 1024, 1024) == (0, 64)
    assert sides.centered_cell_origin(2048, 1152, 1, 1024, 1024) == (1024, 64)
    # 크롭 셀은 캔버스를 좌/우 절반으로 정확히 나누고 겹치지 않는다.
    assert sides.grid_cell_bounds(2048, 1152, 0) == (0, 0, 1024, 1152)
    assert sides.grid_cell_bounds(2048, 1152, 1) == (1024, 0, 2048, 1152)

    for name in ("QUAD_FRONT", "QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS"):
        layout = GROUP_LAYOUTS[name]
        for capture in (1024, 112):
            width, height = layout.canvas_size(capture)
            assert abs(width / height - aspect_value(layout.aspect_ratio)) < 0.01, (name, capture)
            for index in range(layout.cell_count):
                left, bottom, right, top = layout.grid_cell_bounds(width, height, index)
                x, y = layout.centered_cell_origin(width, height, index, capture, capture)
                assert left <= x and x + capture <= right, (name, index)
                assert bottom <= y and y + capture <= top, (name, index)
                assert right - left == width // layout.columns
                assert top - bottom == height // layout.rows
    # 그룹 레이아웃은 별도 레지스트리에 있고 기존 enum 이름 공간을 오염시키지 않는다.
    assert set(TURNAROUND_LAYOUTS) == {"THREE", "SIX"}
    assert resolve_layout("quad_sides") is GROUP_LAYOUTS["QUAD_SIDES"]
    assert "16:9" in ASPECT_RATIO_OPTIONS
    assert {"1:1", "21:9", "3:2"} <= set(ASPECT_RATIO_OPTIONS)
    _expect_value_error(lambda: resolve_layout("QUAD"), "resolve_composition")
    assert resolve_image_size("QUAD", "AUTO") == "2K"
    assert resolve_image_size("QUAD_SIDES", None) == "2K"
    assert resolve_image_size("QUAD", "1k") == "1K"
    _expect_value_error(lambda: resolve_image_size("QUAD", "8K"), ", ".join(IMAGE_SIZE_OPTIONS))


def test_batch_request_keeps_one_call_one_image_per_group() -> None:
    analysis = parse_reference_analysis('{"object_summary":"상자"}')
    batch = build_turnaround_batch_request(
        "QUAD", _quad_contact_sheets(), (), analysis, "푸른 천"
    )

    assert isinstance(batch, TurnaroundBatchRequest)
    assert batch.composition_name == "QUAD"
    assert len(batch.requests) == batch.provider_call_count == MAX_TURNAROUND_GROUPS
    assert batch.views == resolve_composition("QUAD").views
    assert sorted(batch.views) == sorted(ALL_VIEWS)
    assert tuple(request.layout_name for request in batch.requests) == (
        "QUAD_FRONT",
        "QUAD_BACK",
        "QUAD_SIDES",
        "QUAD_CAPS",
    )
    assert tuple(request.aspect_ratio for request in batch.requests) == (
        "1:1",
        "1:1",
        "16:9",
        "16:9",
    )
    assert all(request.image_size == "2K" for request in batch.requests)
    assert all(request.provider_call_count == 1 for request in batch.requests)
    assert all(request.output_image_count == 1 for request in batch.requests)
    # 라운드는 requests 인덱스로 옮겨진다.
    assert batch.rounds == ((0,), (1, 2, 3))
    assert batch.request_for("quad_caps").views == ("TOP", "BOTTOM")
    _expect_value_error(lambda: batch.request_for("QUAD_NOSE"), "묶음에 없는 그룹")

    # 그룹 하나라도 비용 계약을 바꾸면 묶음이 거부된다.
    for changed in ({"provider_call_count": 2}, {"output_image_count": 2}):
        broken = list(batch.requests)
        broken[0] = TurnaroundImageRequestStub(batch.requests[0], **changed)
        _expect_value_error(
            lambda broken=broken: TurnaroundBatchRequest("QUAD", tuple(broken)),
            "1회 호출" if "provider_call_count" in changed else "1장만",
        )
    # 그룹 수, 순서, 시점 합집합이 어긋나면 거부한다.
    _expect_value_error(
        lambda: TurnaroundBatchRequest("QUAD", batch.requests[:3]), "그룹 4개의 요청"
    )
    _expect_value_error(
        lambda: TurnaroundBatchRequest("QUAD", (batch.requests[1], *batch.requests[1:])),
        "그룹 순서와 달라집니다",
    )
    reordered = (batch.requests[0], batch.requests[1], batch.requests[3], batch.requests[2])
    _expect_value_error(
        lambda: TurnaroundBatchRequest("QUAD", reordered), "그룹 순서와 달라집니다"
    )
    _expect_value_error(
        lambda: build_turnaround_batch_request("QUAD", {}, (), analysis, "천"),
        "형상 가이드 이미지가 없습니다",
    )
    _expect_value_error(
        lambda: build_turnaround_batch_request(
            "QUAD", _quad_contact_sheets(), (), analysis, "천",
            regeneration_feedback={"QUAD_NOSE": ("FRONT",)},
        ),
        "구성에 없는 그룹",
    )
    # 단일 캔버스 구성도 같은 자료구조로 표현된다.
    six = build_turnaround_batch_request(
        "SIX",
        {"SIX": InlineImage("image/png", "guide", "geometry_contact_sheet")},
        (),
        analysis,
        "",
    )
    assert len(six.requests) == 1 and six.rounds == ((0,),)
    assert six.requests[0].layout_name == "SIX" and six.requests[0].aspect_ratio == "3:2"


class TurnaroundImageRequestStub:
    """비용 계약 위반을 만들기 위해 계약 검증을 건너뛴 가짜 요청."""

    def __init__(self, source, **overrides) -> None:
        for field in ("layout_name", "views", "provider_call_count", "output_image_count"):
            setattr(self, field, overrides.get(field, getattr(source, field)))


def test_quad_group_prompts_fix_cell_wording_and_front_reference_numbering() -> None:
    analysis = normalize_reference_analysis({"object_summary": "낡은 나무 상자"})

    sides = compile_turnaround_prompt(analysis, "", layout="QUAD_SIDES")
    assert "2시점도 한 장" in sides
    assert "16:9 캔버스를 같은 너비의 2열로 나눕니다" in sides
    assert "왼쪽부터 LEFT SIDE | RIGHT SIDE 순서" in sides
    assert "두 칸을 하나의 넓은 그림으로 합치지 마십시오" in sides
    assert "두 칸 사이에 구분선, 테두리, 배경색 차이를 넣지 않습니다" in sides
    assert "실제 왼쪽 면" in sides

    front = compile_turnaround_prompt(analysis, "", layout="QUAD_FRONT")
    assert "단일 시점도 한 장" in front
    assert "하나의 1:1 캔버스에 시점 하나만 담습니다" in front
    # 1셀에서 "1열로 나눕니다" 같은 어색한 분할 문구가 나오면 안 된다.
    assert "나눕니다" not in front
    assert "1열" not in front
    # 순차 모드의 인페인팅 문장은 라운드1에 칠해진 영역이 없으므로 들어가지 않는다.
    assert "회색(미채색) 영역만" not in front
    assert "이미 일부가 채색된" not in front
    assert "두 칸" not in front
    assert "중앙 정사각형 viewport" in front

    caps = compile_turnaround_prompt(analysis, "", layout="QUAD_CAPS")
    assert "윗줄" not in caps
    assert "왼쪽부터 TOP | BOTTOM 순서" in caps
    assert "정면(FRONT)이 아래쪽" in caps and "정면(FRONT)이 위쪽" in caps

    # FRONT 색 참조가 붙으면 문단이 생기고 사용자 참조 번호가 한 칸 밀린다.
    plain = compile_turnaround_prompt(analysis, "", layout="QUAD_BACK", reference_image_count=2)
    assert "두 번째 이후 이미지 2장(role=palette_only)" in plain
    assert "FRONT 색 기준" not in plain

    shifted = compile_turnaround_prompt(
        analysis, "", layout="QUAD_BACK", reference_image_count=2, front_reference=True
    )
    assert f"- 두 번째 이미지(role={FRONT_COLOR_REFERENCE_ROLE})" in shifted
    assert "세 번째 이후 이미지 2장(role=palette_only)" in shifted
    assert "두 번째 이후 이미지" not in shifted
    assert "FRONT 색 기준(형상 계약 다음으로 우선):" in shifted
    assert "형상은 첫 번째, 색은 두 번째를 따릅니다" in shifted
    # FRONT 참조 문단은 형상 계약 뒤, 스타일 근거 앞에 온다.
    assert shifted.index("형상 계약") < shifted.index("FRONT 색 기준") < shifted.index("분석 JSON")

    # 참조 이미지가 없을 때도 FRONT 참조만으로 번호가 어긋나지 않는다.
    only_front = compile_turnaround_prompt(
        analysis, "", layout="QUAD_SIDES", reference_image_count=0, front_reference=True
    )
    assert f"- 두 번째 이미지(role={FRONT_COLOR_REFERENCE_ROLE})" in only_front
    assert "세 번째" not in only_front
    assert "입력 이미지는 첫 번째 한 장뿐입니다" not in only_front
    # 분석 없이 사용자 지시만 있는 경로도 같은 번호 규칙을 쓴다.
    prompt_only = compile_turnaround_prompt(
        None, "빨간 주사위", layout="QUAD_CAPS", front_reference=True
    )
    assert f"- 두 번째 이미지(role={FRONT_COLOR_REFERENCE_ROLE})" in prompt_only
    assert "이 외의 입력 이미지는 없습니다" not in prompt_only


def test_batch_request_attaches_front_reference_only_to_later_rounds() -> None:
    analysis = parse_reference_analysis('{"object_summary":"상자"}')
    front_image = InlineImage("image/png", "front-result", FRONT_COLOR_REFERENCE_ROLE, "front")
    user_reference = InlineImage("image/jpeg", "palette", "reference", "pinterest")
    batch = build_turnaround_batch_request(
        "QUAD",
        _quad_contact_sheets(),
        (user_reference,),
        analysis,
        "푸른 천",
        front_reference=front_image,
    )

    first = batch.request_for("QUAD_FRONT")
    assert first.reference_images == (user_reference,)
    assert "FRONT 색 기준" not in first.prompt
    assert "두 번째 이후 이미지 1장(role=palette_only)" in first.prompt
    for name in ("QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS"):
        later = batch.request_for(name)
        assert later.reference_images == (front_image, user_reference)
        assert "FRONT 색 기준(형상 계약 다음으로 우선):" in later.prompt
        assert "세 번째 이후 이미지 1장(role=palette_only)" in later.prompt

    # 라운드1을 아직 실행하지 않았으면 참조 없이도 묶음을 만들 수 있다.
    pending = build_turnaround_batch_request(
        "QUAD", _quad_contact_sheets(), (), analysis, "천"
    )
    assert all(request.reference_images == () for request in pending.requests)
    _expect_value_error(
        lambda: build_turnaround_batch_request(
            "QUAD",
            _quad_contact_sheets(),
            (),
            analysis,
            "천",
            front_reference=InlineImage("image/png", "front-result", "reference"),
        ),
        f"역할은 {FRONT_COLOR_REFERENCE_ROLE}",
    )


def test_composition_declares_its_color_reference_view() -> None:
    """뒤 라운드가 색을 맞출 기준 시점은 위치가 아니라 구성이 명시한다."""

    quad = resolve_composition("QUAD")
    assert quad.color_reference_view == "FRONT"
    assert quad.reference_view == "FRONT"
    # 기준 시점은 첫 라운드에서 반드시 확보되는 시점이어야 한다.
    assert quad.reference_view in quad.group(quad.rounds[0][0]).views
    # 선언하지 않은 구성은 첫 라운드 첫 그룹의 첫 시점을 쓴다(기존 동작).
    six = resolve_composition("SIX")
    assert six.color_reference_view == ""
    assert six.reference_view == six.groups[0].views[0] == "FRONT"
    _expect_value_error(
        lambda: TurnaroundComposition(
            "BADREF",
            (GROUP_LAYOUTS["QUAD_FRONT"], GROUP_LAYOUTS["QUAD_CAPS"]),
            (("QUAD_FRONT",), ("QUAD_CAPS",)),
            "TOP",
        ),
        "첫 라운드 그룹의 시점",
    )


def test_inline_image_rejects_unknown_roles() -> None:
    assert INLINE_IMAGE_ROLES == (
        "geometry_contact_sheet",
        "reference",
        FRONT_COLOR_REFERENCE_ROLE,
    )
    assert InlineImage("image/png", "data").role == "reference"
    for role in INLINE_IMAGE_ROLES:
        assert InlineImage("image/png", "data", role).role == role
    _expect_value_error(
        lambda: InlineImage("image/png", "data", "palette_only"), "인라인 이미지 역할은"
    )
    _expect_value_error(
        lambda: build_turnaround_request(
            InlineImage("image/png", "data", "reference"), [], None, "지시"
        ),
        "contact sheet의 역할",
    )


if __name__ == "__main__":
    main()
