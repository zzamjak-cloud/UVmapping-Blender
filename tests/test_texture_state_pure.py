"""Blender 없이 실행하는 다면도 상태·그룹 실행 계획 테스트.

연산자는 bpy 없이는 import되지 않으므로, 구버전 상태 해석·그룹 경로 규칙·재생성
대상 계산·상태 1.4 구조처럼 순수하게 결정되는 부분만 여기서 고정한다.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_pipeline import ALL_VIEWS, resolve_composition
from uvmapping.texture_state import (
    FAILED_GROUPS_STATUS,
    SINGLE_CANVAS_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    build_group_entries,
    build_turnaround_state,
    design_signature,
    group_output_path,
    group_sheet_path,
    merged_view_mapping,
    referenced_paths,
    regeneration_feedback_by_group,
    split_rounds,
    stale_artifacts,
    state_composition,
    views_cover_composition,
)


QUAD = resolve_composition("QUAD")
SIX = resolve_composition("SIX")
STEM = Path("/tmp/uvmapping/box_turnaround.png")


def _expect_error(action, fragment: str) -> None:
    try:
        action()
    except ValueError as error:
        assert fragment in str(error), f"예상 밖 오류 문구입니다: {error}"
        return
    raise AssertionError(f"오류가 발생하지 않았습니다: {fragment}")


def test_legacy_states_resolve_without_migration() -> None:
    # 1.0~1.3 상태는 composition 필드가 없다. layout 이름이 곧 구성 이름이어야 한다.
    assert state_composition({"schema_version": "1.3", "layout": "SIX"}).name == "SIX"
    assert state_composition({"schema_version": "1.2", "layout": "THREE"}).name == "THREE"
    # 레이아웃조차 없는 최초 스키마는 3면도로 해석한다.
    assert state_composition({"schema_version": "1.0"}).name == "THREE"
    assert state_composition({"layout": ""}).name == "THREE"


def test_state_composition_prefers_explicit_composition_field() -> None:
    state = {"schema_version": STATE_SCHEMA_VERSION, "layout": "QUAD", "composition": "QUAD"}
    assert state_composition(state).name == "QUAD"
    assert state_composition(state).views == ("FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM")
    assert sorted(state_composition(state).views) == sorted(ALL_VIEWS)


def test_design_signature_separates_group_results() -> None:
    left = {"turnaround_sha256": "aa", "views": {"front": "a.png"}, "groups": [{"name": "QUAD_FRONT"}]}
    right = {"turnaround_sha256": "aa", "views": {"front": "a.png"}, "groups": [{"name": "QUAD_BACK"}]}
    assert design_signature(left) != design_signature(right)
    # 그룹이 없는 구버전 상태끼리는 기존 기준(대표 해시 + 크롭 매핑)만으로 비교된다.
    legacy = {"turnaround_sha256": "aa", "views": {"front": "a.png"}}
    assert design_signature(legacy) == design_signature(dict(legacy))


def test_group_paths_do_not_collide() -> None:
    sheets = {group.name: group_sheet_path(STEM, group.name) for group in QUAD.groups}
    outputs = {group.name: group_output_path(STEM, group.name) for group in QUAD.groups}
    retries = {group.name: group_output_path(STEM, group.name, 2) for group in QUAD.groups}
    every = [*sheets.values(), *outputs.values(), *retries.values()]
    assert len({str(path) for path in every}) == len(every)
    assert outputs["QUAD_FRONT"].name == "box_turnaround_quad_front.png"
    assert sheets["QUAD_SIDES"].name == "box_turnaround_quad_sides_geometry.png"
    assert retries["QUAD_CAPS"].name == "box_turnaround_quad_caps_retry02.png"
    # 크롭은 결과 파일 stem 뒤에 시점을 붙이므로 그룹 간에도 겹치지 않는다.
    crops = {
        f"{outputs[group.name].stem}_{view.lower()}.png"
        for group in QUAD.groups
        for view in group.views
    }
    assert len(crops) == len(QUAD.views)


def test_split_rounds_keeps_front_first() -> None:
    assert split_rounds(QUAD, QUAD.group_names) == (
        ("QUAD_FRONT",),
        ("QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS"),
    )
    # FRONT가 포함되면 먼저 만들고 나머지가 그 결과를 참조한다.
    assert split_rounds(QUAD, ("QUAD_SIDES", "QUAD_FRONT")) == (
        ("QUAD_FRONT",),
        ("QUAD_SIDES",),
    )
    # FRONT가 빠지면 라운드가 하나로 줄어 기존 FRONT 크롭을 참조로 병렬 처리된다.
    assert split_rounds(QUAD, ("QUAD_CAPS", "QUAD_BACK")) == (("QUAD_BACK", "QUAD_CAPS"),)
    assert split_rounds(SIX, ("SIX",)) == (("SIX",),)
    assert split_rounds(QUAD, ()) == ()
    _expect_error(lambda: split_rounds(QUAD, ("SIX",)), "구성에 없는 그룹")


def test_regeneration_maps_failed_views_to_groups() -> None:
    assert regeneration_feedback_by_group(QUAD, ("LEFT",)) == {"QUAD_SIDES": ("LEFT",)}
    # 같은 캔버스의 두 시점이 함께 실패해도 그룹은 하나다.
    assert regeneration_feedback_by_group(QUAD, ("TOP", "BOTTOM")) == {
        "QUAD_CAPS": ("TOP", "BOTTOM")
    }
    assert regeneration_feedback_by_group(QUAD, ("FRONT", "RIGHT")) == {
        "QUAD_FRONT": ("FRONT",),
        "QUAD_SIDES": ("RIGHT",),
    }
    # 단일 캔버스 구성은 모든 시점이 한 그룹이다.
    assert regeneration_feedback_by_group(SIX, ("TOP", "FRONT")) == {"SIX": ("TOP", "FRONT")}
    _expect_error(lambda: regeneration_feedback_by_group(QUAD, ("NOSE",)), "구성에 없는 시점")


def test_merged_view_mapping_preserves_untouched_groups() -> None:
    previous = {"front": "f0.png", "left": "l0.png", "right": "r0.png"}
    merged = merged_view_mapping(previous, {"LEFT": "l1.png", "RIGHT": "r1.png"})
    assert merged == {"front": "f0.png", "left": "l1.png", "right": "r1.png"}
    assert previous["left"] == "l0.png", "원본 매핑을 변경하면 안 됩니다."


def _quad_state(status: str = "TURNAROUND_READY") -> dict:
    image_paths = {group.name: f"{group.name.lower()}.png" for group in QUAD.groups}
    sheet_paths = {group.name: f"{group.name.lower()}_geometry.png" for group in QUAD.groups}
    entries = build_group_entries(
        QUAD,
        image_paths=image_paths,
        image_hashes={name: f"sha-{name}" for name in image_paths},
        sheet_paths=sheet_paths,
        sheet_hashes={name: f"guide-{name}" for name in sheet_paths},
        image_size="2K",
        attempts={"QUAD_SIDES": 1},
        feedback={"QUAD_SIDES": ("LEFT",)},
    )
    return build_turnaround_state(
        {"model": "google/gemini-3-pro-image", "projection": {"center": [0, 0, 0]}},
        QUAD,
        entries,
        {view: f"{view.lower()}_crop.png" for view in QUAD.views},
        {view: f"crop-{view.lower()}" for view in QUAD.views},
        status=status,
        attempt=1,
        feedback_views=("LEFT",),
    )


def test_state_1_4_structure() -> None:
    state = _quad_state()
    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["composition"] == "QUAD"
    # layout 필드는 구버전 독자를 위해 구성 이름을 그대로 담는다.
    assert state["layout"] == "QUAD"
    assert state["model"] == "google/gemini-3-pro-image", "base 필드는 보존되어야 합니다."
    assert [entry["name"] for entry in state["groups"]] == list(QUAD.group_names)
    assert [entry["round"] for entry in state["groups"]] == [0, 1, 1, 1]
    assert state["groups"][2]["attempt"] == 1
    assert state["groups"][2]["regeneration_feedback"] == ["LEFT"]
    assert state["groups"][2]["views"] == ["LEFT", "RIGHT"]
    assert state["groups"][2]["aspect_ratio"] == "16:9"
    assert state["groups"][0]["aspect_ratio"] == "1:1"
    # 하위 호환 대표값은 첫 그룹(FRONT) 이미지다.
    assert state["turnaround_path"] == "quad_front.png"
    assert state["turnaround_sha256"] == "sha-QUAD_FRONT"
    assert state["geometry_contact_sheet"] == "quad_front_geometry.png"
    # 평탄 시점 매핑은 1.3과 같은 형태를 유지한다.
    assert set(state["views"]) == {view.lower() for view in ALL_VIEWS}
    assert set(state["view_sha256"]) == set(state["views"])
    assert state["attempt"] == 1
    assert state["regeneration_feedback"] == ["LEFT"]


def test_failed_group_state_is_not_bakeable() -> None:
    state = _quad_state(FAILED_GROUPS_STATUS)
    assert state["status"] == FAILED_GROUPS_STATUS
    assert state["status"] not in {"TURNAROUND_READY", "ALBEDO_APPLIED"}
    # 실패해도 성공 그룹의 크롭은 상태에 남는다.
    assert state["views"]["front"] == "front_crop.png"


def test_state_builder_rejects_empty_groups() -> None:
    _expect_error(
        lambda: build_turnaround_state({}, QUAD, (), {}, {}, status="TURNAROUND_READY"),
        "그룹이 하나도 없습니다",
    )


def test_views_cover_composition_detects_incomplete_results() -> None:
    complete = {view.lower(): f"{view.lower()}.png" for view in QUAD.views}
    assert views_cover_composition(QUAD, complete) is True
    # 첫 라운드만 끝난 상태는 아직 완전하지 않다.
    assert views_cover_composition(QUAD, {"front": "front.png"}) is False
    # 경로가 빈 문자열이면 있는 것으로 치지 않는다.
    assert views_cover_composition(QUAD, {**complete, "left": ""}) is False
    assert views_cover_composition(SIX, {view.lower(): "x.png" for view in SIX.views}) is True


def test_referenced_paths_covers_every_state_field() -> None:
    state = _quad_state()
    referenced = referenced_paths(state)
    assert state["turnaround_path"] in referenced
    assert state["geometry_contact_sheet"] in referenced
    for entry in state["groups"]:
        assert entry["image_path"] in referenced
        assert entry["geometry_contact_sheet"] in referenced
    for path in state["views"].values():
        assert path in referenced
    # 빈 값은 후보에 들어가지 않아야 한다(모든 파일을 지우는 사고 방지).
    assert "" not in referenced


def test_stale_artifacts_never_touch_referenced_files() -> None:
    state = _quad_state()
    referenced = sorted(referenced_paths(state))
    stale = ["old_quad_sides.png", "old_quad_sides_left.png", "old_quad_sides_right.png"]
    result = stale_artifacts([*referenced, *stale], state)
    assert sorted(str(path) for path in result) == sorted(stale)
    # 상태가 참조하는 파일만 주면 지울 것이 없다.
    assert stale_artifacts(referenced, state) == ()


def test_stale_artifacts_keep_retry_results_and_drop_previous_attempt() -> None:
    with tempfile.TemporaryDirectory(prefix="uvmapping-stale-") as temporary:
        root = Path(temporary)
        stem = root / "box_turnaround.png"
        # 첫 시도와 재시도 산출물이 같은 stem 아래 함께 남아 있는 상황.
        previous = group_output_path(stem, "QUAD_SIDES")
        retried = group_output_path(stem, "QUAD_SIDES", 1)
        files = [
            previous,
            previous.with_name(f"{previous.stem}_left.png"),
            retried,
            retried.with_name(f"{retried.stem}_left.png"),
            group_sheet_path(stem, "QUAD_SIDES"),
        ]
        for path in files:
            path.write_bytes(b"x")
        state = {
            "views": {"left": str(retried.with_name(f"{retried.stem}_left.png"))},
            "groups": [
                {
                    "name": "QUAD_SIDES",
                    "image_path": str(retried),
                    "geometry_contact_sheet": str(group_sheet_path(stem, "QUAD_SIDES")),
                }
            ],
        }
        removable = stale_artifacts(sorted(root.glob(f"{stem.stem}_*")), state)
        assert sorted(path.name for path in removable) == sorted(
            [previous.name, f"{previous.stem}_left.png"]
        )
        assert retried not in removable and group_sheet_path(stem, "QUAD_SIDES") not in removable


def test_single_canvas_schema_version_is_unchanged() -> None:
    # 단일 캔버스 구성은 회귀를 피하려 기존 1.3을 그대로 쓴다.
    assert SINGLE_CANVAS_SCHEMA_VERSION == "1.3"
    assert STATE_SCHEMA_VERSION == "1.4"


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 다면도 상태 테스트 {len(tests)}/{len(tests)} 통과")
