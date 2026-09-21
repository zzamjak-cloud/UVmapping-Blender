"""다면도 생성 상태와 그룹 단위 실행 계획을 bpy 없이 다루는 순수 계층.

연산자(`texture_operators`)는 Blender 없이는 import조차 되지 않으므로, 상태 스키마
해석·경로 규칙·재생성 대상 계산처럼 순수하게 결정되는 부분만 여기로 분리해 순수
테스트로 고정한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .texture_pipeline import TurnaroundComposition, resolve_composition


# 그룹 여러 개를 쓰는 구성(QUAD)이 기록하는 스키마. 단일 캔버스 구성은 1.3을 그대로 쓴다.
STATE_SCHEMA_VERSION = "1.4"
SINGLE_CANVAS_SCHEMA_VERSION = "1.3"
# 그룹 일부가 실패해 성공분만 남은 상태. 베이크 가능 상태 목록에 넣지 않아 검증에서 걸린다.
FAILED_GROUPS_STATUS = "TURNAROUND_FAILED_GROUPS"


def state_composition(state: Mapping[str, Any]) -> TurnaroundComposition:
    """1.0~1.4 상태에서 구성을 해석한다.

    1.4 이상은 ``composition``을, 그 이전은 ``layout``(THREE/SIX)을 구성 이름으로
    본다. 둘 다 없는 최초 스키마는 ``resolve_composition(None)``과 같은 3면도다.
    """

    return resolve_composition(state.get("composition") or state.get("layout"))


def design_signature(state: Mapping[str, Any]) -> tuple[str, str, str]:
    """여러 객체가 같은 다면도 디자인을 쓰는지 비교할 지문.

    그룹이 여러 장인 구성은 대표 이미지 해시 하나로 서로 다른 생성 결과를 구분하지
    못하므로 그룹 목록까지 지문에 넣는다.
    """

    return (
        str(state.get("turnaround_sha256", "")),
        json.dumps(state.get("views", {}), sort_keys=True),
        json.dumps(state.get("groups"), sort_keys=True),
    )


def group_sheet_path(stem_path: Path, group_name: str) -> Path:
    """그룹별 형상 가이드(contact sheet) 경로."""

    return Path(stem_path).with_name(f"{Path(stem_path).stem}_{str(group_name).lower()}_geometry.png")


def group_output_path(stem_path: Path, group_name: str, attempt: int = 0) -> Path:
    """그룹별 생성 결과 경로. 재생성은 시도 번호를 붙여 앞 결과를 덮지 않는다."""

    stem = Path(stem_path).stem
    suffix = f"_retry{int(attempt):02d}" if int(attempt) > 0 else ""
    return Path(stem_path).with_name(f"{stem}_{str(group_name).lower()}{suffix}.png")


def split_rounds(
    composition: TurnaroundComposition, group_names: Sequence[str]
) -> tuple[tuple[str, ...], ...]:
    """대상 그룹을 구성의 라운드 순서로 나눈다.

    뒤 라운드는 앞 라운드 결과를 색 참조로 받으므로, 부분 재생성에서도 이 순서를
    지켜야 FRONT 참조가 항상 최신 결과를 가리킨다. 비어 있는 라운드는 버린다.
    """

    wanted = {str(name).upper() for name in group_names}
    unknown = sorted(wanted - set(composition.group_names))
    if unknown:
        raise ValueError(f"{composition.name} 구성에 없는 그룹입니다: {', '.join(unknown)}")
    rounds = tuple(
        tuple(name for name in round_names if name in wanted)
        for round_names in composition.rounds
    )
    return tuple(round_names for round_names in rounds if round_names)


def regeneration_feedback_by_group(
    composition: TurnaroundComposition, failed_views: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """실패 시점을 소속 그룹으로 묶는다. 그룹 하나가 캔버스 하나이므로 재생성 단위도 그룹이다."""

    grouped: dict[str, list[str]] = {}
    for view in failed_views:
        name = str(view).upper()
        group = composition.group_of_view(name)
        grouped.setdefault(group.name, [])
        if name not in grouped[group.name]:
            grouped[group.name].append(name)
    return {name: tuple(views) for name, views in grouped.items()}


def views_cover_composition(
    composition: TurnaroundComposition, view_paths: Mapping[str, str]
) -> bool:
    """구성의 모든 시점이 크롭 경로를 갖고 있는지. 파일 존재 여부는 호출 측이 본다."""

    available = {str(key).lower(): str(value) for key, value in view_paths.items()}
    return all(available.get(view.lower()) for view in composition.views)


def referenced_paths(state: Mapping[str, Any]) -> frozenset[str]:
    """상태가 가리키는 모든 파일 경로. 정리 대상에서 제외할 기준이다."""

    paths = {
        str(state.get("turnaround_path", "")),
        str(state.get("geometry_contact_sheet", "")),
        str(state.get("albedo_path", "")),
    }
    paths.update(str(value) for value in (state.get("views") or {}).values())
    paths.update(str(value) for value in (state.get("guides") or {}).values())
    for entry in state.get("groups") or ():
        if not isinstance(entry, Mapping):
            continue
        paths.add(str(entry.get("image_path", "")))
        paths.add(str(entry.get("geometry_contact_sheet", "")))
    return frozenset(path for path in paths if path)


def stale_artifacts(candidates, state: Mapping[str, Any]) -> tuple[Path, ...]:
    """상태가 참조하지 않는 중간 산출물만 골라낸다.

    재생성은 ``_retryNN`` 캔버스와 그 크롭을 새로 만들고 이전 시도 파일은 남긴다.
    상태가 가리키는 파일은 어떤 경우에도 후보에서 빠져야 한다.
    """

    keep = referenced_paths(state)
    return tuple(Path(path) for path in candidates if str(path) not in keep)


def merged_view_mapping(
    previous: Mapping[str, str], updates: Mapping[str, str]
) -> dict[str, str]:
    """재생성하지 않은 그룹의 크롭을 보존한 채 새 결과만 덮어쓴다."""

    merged = {str(key).lower(): str(value) for key, value in previous.items()}
    merged.update({str(key).lower(): str(value) for key, value in updates.items()})
    return merged


def build_group_entries(
    composition: TurnaroundComposition,
    *,
    image_paths: Mapping[str, str],
    image_hashes: Mapping[str, str],
    sheet_paths: Mapping[str, str],
    sheet_hashes: Mapping[str, str],
    image_size: str,
    attempts: Mapping[str, int] | None = None,
    feedback: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """상태 1.4의 ``groups`` 배열. 순서는 구성의 그룹 순서를 그대로 따른다."""

    attempts = attempts or {}
    feedback = feedback or {}
    entries: list[dict[str, Any]] = []
    for group in composition.groups:
        entries.append(
            {
                "name": group.name,
                "views": list(group.views),
                "aspect_ratio": group.aspect_ratio,
                "image_size": str(image_size),
                "round": composition.round_index(group.name),
                "attempt": int(attempts.get(group.name, 0)),
                "image_path": str(image_paths.get(group.name, "")),
                "image_sha256": str(image_hashes.get(group.name, "")),
                "geometry_contact_sheet": str(sheet_paths.get(group.name, "")),
                "geometry_sha256": str(sheet_hashes.get(group.name, "")),
                "regeneration_feedback": [str(view) for view in feedback.get(group.name, ())],
            }
        )
    return entries


def build_turnaround_state(
    base: Mapping[str, Any],
    composition: TurnaroundComposition,
    groups: Sequence[Mapping[str, Any]],
    views: Mapping[str, str],
    view_sha256: Mapping[str, str],
    *,
    status: str,
    attempt: int = 0,
    feedback_views: Sequence[str] = (),
) -> dict[str, Any]:
    """그룹 구성의 상태 1.4를 만든다.

    ``views``/``view_sha256``(소문자 시점 → 크롭 경로/해시) 평탄 매핑은 1.3과 동일한
    형태를 유지한다. 베이크와 상태 검증이 이 매핑만 읽으므로 그룹 도입이 그쪽 계약을
    건드리지 않는다. ``turnaround_path``는 첫 그룹 이미지를 가리켜 구버전 독자도
    대표 결과 한 장을 찾을 수 있게 한다.
    """

    if not groups:
        raise ValueError("상태에 기록할 그룹이 하나도 없습니다.")
    representative = groups[0]
    state = dict(base)
    state.update(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": status,
            "layout": composition.name,
            "composition": composition.name,
            "groups": [dict(entry) for entry in groups],
            "geometry_contact_sheet": str(representative.get("geometry_contact_sheet", "")),
            "geometry_sha256": str(representative.get("geometry_sha256", "")),
            "turnaround_path": str(representative.get("image_path", "")),
            "turnaround_sha256": str(representative.get("image_sha256", "")),
            "attempt": int(attempt),
            "regeneration_feedback": [str(view) for view in feedback_views],
            "views": {str(key).lower(): str(value) for key, value in views.items()},
            "view_sha256": {str(key).lower(): str(value) for key, value in view_sha256.items()},
        }
    )
    return state
