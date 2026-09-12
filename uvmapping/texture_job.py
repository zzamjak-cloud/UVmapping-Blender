"""사용자가 이미 만들어 둔 UV에서 AI 텍스처 계약(TextureJob)을 생성한다.

애드온이 UV를 직접 펼치지 않으므로, 3면도 생성 직전에 현재 활성 UV 레이어를
그대로 읽어 Atlas manifest와 객체별 TextureJob을 만든다. 계약은 UV·메시 내용만으로
결정되므로 같은 상태에서 다시 계산하면 동일한 해시가 나오고, 그 성질이 베이크
단계의 "3면도 생성 이후 UV가 바뀌었는지" 검사 근거가 된다.
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import build_atlas_context, build_texture_job, count_uv_islands
from .quality import OVERLAP_EXACT, evaluate_atlas_quality, evaluate_uv_quality


TEXTURE_JOB_PROPERTY = "uvmapping_texture_job"
TEXTURE_TARGET_UDIMS = (1001,)
ADDON_VERSION = "2.3.0"

# 사용자 UV를 그대로 쓰므로 member 간 겹침 판정은 예산을 넉넉히 잡아 정확히 센다.
_ATLAS_PAIR_BUDGET = 1_000_000


def _active_uv_layer_name(mesh: Any, mesh_name: str) -> str:
    """베이크와 계약이 공유할 활성 UV 레이어 이름을 확정한다."""

    layers = getattr(mesh, "uv_layers", None)
    active = getattr(layers, "active", None) if layers is not None else None
    name = str(getattr(active, "name", "") or "")
    if not name:
        raise ValueError(
            f"{mesh_name}: UV 맵이 없습니다. Blender에서 UV를 펼친 뒤 다시 시도해 주세요."
        )
    return name


def _seam_edges(mesh: Any) -> tuple[int, ...]:
    """UV 아일랜드 경계를 계약에 기록하기 위해 현재 Seam Edge를 수집한다."""

    return tuple(
        int(getattr(edge, "index", position))
        for position, edge in enumerate(getattr(mesh, "edges", ()))
        if getattr(edge, "use_seam", False)
    )


def _group_by_mesh(objects: tuple) -> tuple[dict[str, list], dict[str, Any]]:
    """링크 복제본이 하나의 Atlas member가 되도록 Mesh 데이터블록 기준으로 묶는다."""

    groups: dict[str, list] = {}
    meshes: dict[str, Any] = {}
    for obj in objects:
        mesh = obj.data
        key = str(getattr(mesh, "name_full", None) or mesh.name)
        groups.setdefault(key, []).append(obj)
        meshes[key] = mesh
    return groups, meshes


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_texture_jobs(
    objects: tuple,
    *,
    resolution: int,
    padding: int,
) -> dict[int, dict[str, Any]]:
    """선택 객체들의 현재 UV로 하나의 공유 Atlas 계약과 객체별 TextureJob을 만든다.

    반환 값은 ``obj.as_pointer()`` 를 키로 하는 TextureJob 사전이다.
    """

    if not objects:
        raise ValueError("텍스처를 만들 Mesh 객체를 선택해 주세요.")
    target_resolution = (int(resolution), int(resolution))
    normalized_padding = int(padding)
    packing_margin = normalized_padding / min(target_resolution)

    groups, meshes = _group_by_mesh(objects)
    shared = len(groups) > 1

    entries = []
    descriptors = []
    layer_names: dict[str, str] = {}
    seam_map: dict[str, tuple[int, ...]] = {}
    for key in sorted(groups):
        mesh = meshes[key]
        layer_name = _active_uv_layer_name(mesh, key)
        seams = _seam_edges(mesh)
        layer_names[key] = layer_name
        seam_map[key] = seams
        entries.append((key, mesh, layer_name))
        descriptors.append(
            {
                "member_id": key,
                "island_count": count_uv_islands(mesh, layer_name, seams),
                "mesh_name": key,
                "uv_layer_name": layer_name,
                "object_names": tuple(
                    sorted(
                        str(getattr(obj, "name_full", None) or obj.name)
                        for obj in groups[key]
                    )
                ),
            }
        )

    report = evaluate_atlas_quality(entries, max_pair_checks=_ATLAS_PAIR_BUDGET)
    if report.out_of_bounds_count:
        raise ValueError(
            "UV가 0-1 범위를 벗어났습니다. UV 에디터에서 UV > Pack Islands로 "
            f"0-1 안에 배치해 주세요. (범위 밖 member {report.out_of_bounds_count}개)"
        )
    if shared and report.overlap_status == OVERLAP_EXACT and report.overlap_pairs:
        raise ValueError(
            "선택한 객체들의 UV가 서로 겹칩니다. 한 장의 텍스처에 함께 구우려면 "
            "겹치지 않게 배치하거나 객체를 하나씩 선택해 주세요. "
            f"(겹치는 삼각형 {report.overlap_pairs}쌍)"
        )
    atlas_context = build_atlas_context(report, descriptors, shared=shared)

    jobs: dict[int, dict[str, Any]] = {}
    for descriptor in descriptors:
        key = str(descriptor["member_id"])
        mesh = meshes[key]
        layer_name = layer_names[key]
        seams = seam_map[key]
        quality = evaluate_uv_quality(
            mesh,
            uv_layer_name=layer_name,
            seam_count=len(seams),
            chart_count=int(descriptor["island_count"]),
        )
        job_settings = {
            "uv_source": "USER_AUTHORED",
            "uv_layer_name": layer_name,
            "packing_margin_method": "FRACTION",
            "packing_margin_uv": packing_margin,
            "pack_shared_atlas": shared,
            "generation_target": {
                "resolution": target_resolution,
                "padding_pixels": normalized_padding,
                "udim_tiles": TEXTURE_TARGET_UDIMS,
            },
        }
        for obj in groups[key]:
            job = build_texture_job(
                mesh,
                str(getattr(obj, "name_full", None) or obj.name),
                layer_name,
                quality,
                seams,
                addon_version=ADDON_VERSION,
                resolution=target_resolution,
                padding=normalized_padding,
                udim_tiles=TEXTURE_TARGET_UDIMS,
                object_transform=tuple(tuple(row) for row in obj.matrix_world),
                coordinate_space="OBJECT",
                settings=job_settings,
                packing_margin_method="FRACTION",
                packing_margin_uv=packing_margin,
                pack_shared_atlas=shared,
                atlas_context=atlas_context,
                atlas_member_id=key,
            )
            payload = job.to_dict()
            payload["settings"] = job_settings
            jobs[obj.as_pointer()] = payload
    return jobs


def ensure_texture_jobs(objects: tuple, settings: Any) -> tuple[dict[str, Any], ...]:
    """현재 UV로 TextureJob을 다시 계산해 객체 속성에 저장하고 순서대로 반환한다."""

    jobs = build_texture_jobs(
        objects,
        resolution=int(settings.texture_resolution),
        padding=int(settings.padding_pixels),
    )
    ordered = []
    for obj in objects:
        payload = jobs[obj.as_pointer()]
        obj[TEXTURE_JOB_PROPERTY] = _json_text(payload)
        ordered.append(payload)
    return tuple(ordered)


__all__ = (
    "ADDON_VERSION",
    "TEXTURE_JOB_PROPERTY",
    "TEXTURE_TARGET_UDIMS",
    "build_texture_jobs",
    "ensure_texture_jobs",
)
