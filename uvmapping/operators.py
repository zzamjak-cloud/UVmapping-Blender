"""Blender 메시 분석과 자동 UV 언랩 연산자."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from math import isfinite
from typing import Any

import bmesh
import bpy
from bpy.props import IntProperty
from bpy.types import Operator

from .analysis import analyze_mesh, generate_analysis_candidates
from .contracts import build_atlas_context, build_texture_job
from .quality import (
    OVERLAP_BUDGET_EXCEEDED,
    OVERLAP_EXACT,
    evaluate_atlas_quality,
    evaluate_uv_quality,
)
from .types import AnalysisOptions


PREVIEW_ATTRIBUTE = "_uvmapping_preview_seam"
QUALITY_PROPERTY = "uvmapping_quality"
TEXTURE_JOB_PROPERTY = "uvmapping_texture_job"
TEXTURE_TARGET_UDIMS = (1001,)
ATLAS_PROXY_PROPERTY = "_uvmapping_atlas_proxy"
_MISSING = object()


@dataclass(slots=True)
class _TargetGroup:
    """같은 원본 Mesh datablock을 사용하는 선택 객체 묶음."""

    source_mesh: Any
    objects: tuple[Any, ...]
    all_users: tuple[Any, ...]

    @property
    def has_unselected_users(self) -> bool:
        return len(self.objects) < len(self.all_users)


@dataclass(slots=True)
class _CandidateArtifact:
    """원본과 분리된 scratch Mesh에서 선택된 최종 후보."""

    group: _TargetGroup
    mesh: Any
    analysis: Any
    quality: Any
    uv_layer_name: str
    method: str
    candidate_index: int
    atlas_member_id: str = ""
    atlas_report: Any = None
    atlas_context: dict[str, Any] | None = None


def _active_mesh_object(context):
    obj = context.view_layer.objects.active
    if obj is None or obj.type != "MESH" or obj.data is None:
        return None
    return obj


def _target_objects(context, settings):
    if settings.process_selected_objects:
        candidates = getattr(context, "selected_editable_objects", None)
        if candidates is None:
            candidates = context.selected_objects
    else:
        active = _active_mesh_object(context)
        candidates = (active,) if active is not None else ()

    targets = {
        obj.as_pointer(): obj
        for obj in candidates
        if obj is not None
        and obj.type == "MESH"
        and obj.data is not None
        and len(obj.data.polygons) > 0
    }
    return tuple(sorted(targets.values(), key=lambda obj: obj.name_full))


def _group_targets(objects):
    grouped: dict[int, list[Any]] = {}
    meshes: dict[int, Any] = {}
    for obj in objects:
        key = obj.data.as_pointer()
        grouped.setdefault(key, []).append(obj)
        meshes[key] = obj.data

    groups = []
    for key, selected in grouped.items():
        mesh = meshes[key]
        all_users = tuple(
            sorted(
                (
                    obj
                    for obj in bpy.data.objects
                    if obj.type == "MESH" and obj.data == mesh
                ),
                key=lambda obj: obj.name_full,
            )
        )
        groups.append(
            _TargetGroup(
                source_mesh=mesh,
                objects=tuple(sorted(selected, key=lambda obj: obj.name_full)),
                all_users=all_users,
            )
        )
    return tuple(
        sorted(groups, key=lambda group: (group.objects[0].name_full, group.source_mesh.name_full))
    )


def _build_analysis_options(settings):
    options = AnalysisOptions.for_preset(settings.preset)
    overrides = {
        "preserve_existing_seams": settings.seam_policy == "PRESERVE",
        "ensure_cut_paths": settings.ensure_cut_paths,
        "connect_boundary_loops": settings.connect_boundary_loops,
    }
    if settings.use_custom_analysis:
        overrides.update(
            angle_weight=settings.angle_weight,
            concave_multiplier=settings.concave_multiplier,
            sharp_weight=settings.sharp_weight,
            material_weight=settings.material_weight,
            seam_threshold=settings.seam_threshold,
            min_chart_faces=settings.min_chart_faces,
        )
    return replace(options, **overrides)


def _capture_seams(mesh):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        return tuple(edge.seam for edge in bm.edges)
    finally:
        bm.free()


def _write_seams(mesh, seam_edges, preserve):
    seam_indices = set(seam_edges)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        for edge in bm.edges:
            edge.seam = edge.index in seam_indices or (preserve and edge.seam)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()


def _capture_mesh_selection(mesh):
    uv_selection = {}
    for layer in mesh.uv_layers:
        uv_selection[layer.name] = tuple(
            (
                bool(getattr(loop, "select", False)),
                bool(getattr(loop, "select_edge", False)),
            )
            for loop in layer.data
        )
    return {
        "vertices": tuple(vertex.select for vertex in mesh.vertices),
        "edges": tuple(edge.select for edge in mesh.edges),
        "polygons": tuple(polygon.select for polygon in mesh.polygons),
        "uv_selection": uv_selection,
    }


def _restore_mesh_selection(mesh, snapshot):
    for item, selected in zip(mesh.vertices, snapshot["vertices"], strict=False):
        item.select = selected
    for item, selected in zip(mesh.edges, snapshot["edges"], strict=False):
        item.select = selected
    for item, selected in zip(mesh.polygons, snapshot["polygons"], strict=False):
        item.select = selected

    saved_layers = snapshot["uv_selection"]
    for layer in mesh.uv_layers:
        saved = saved_layers.get(layer.name)
        if saved is None:
            saved = ((False, False),) * len(layer.data)
        for loop, (selected, edge_selected) in zip(layer.data, saved, strict=False):
            if hasattr(loop, "select"):
                loop.select = selected
            if hasattr(loop, "select_edge"):
                loop.select_edge = edge_selected
    mesh.update()


def _select_uv_layer(mesh, settings):
    name = settings.uv_layer_name.strip() or "AutoUV"
    if settings.create_new_uv_layer:
        layer = mesh.uv_layers.new(name=name, do_init=False)
    else:
        layer = mesh.uv_layers.get(name) or mesh.uv_layers.active
        if layer is None:
            layer = mesh.uv_layers.new(name=name, do_init=False)
    mesh.uv_layers.active_index = tuple(mesh.uv_layers).index(layer)
    return layer


def _unwrap_method():
    method_property = bpy.ops.uv.unwrap.get_rna_type().properties["method"]
    methods = {item.identifier for item in method_property.enum_items}
    return "MINIMUM_STRETCH" if "MINIMUM_STRETCH" in methods else "ANGLE_BASED"


def _require_finished(result, label):
    if "FINISHED" not in result:
        raise RuntimeError(f"{label} 연산이 완료되지 않았습니다: {sorted(result)}")


def _run_uv_pipeline(settings):
    method = _unwrap_method()
    unwrap_args = {
        "method": method,
        "fill_holes": settings.fill_holes,
        "correct_aspect": settings.correct_aspect,
        "margin": 0.0,
    }
    if method == "MINIMUM_STRETCH":
        unwrap_args["iterations"] = settings.unwrap_iterations

    result = bpy.ops.uv.unwrap(**unwrap_args)
    if "FINISHED" not in result and method == "MINIMUM_STRETCH":
        unwrap_args.pop("iterations", None)
        method = "ANGLE_BASED"
        unwrap_args["method"] = method
        result = bpy.ops.uv.unwrap(**unwrap_args)
    _require_finished(result, "UV 언랩")
    _require_finished(
        bpy.ops.uv.average_islands_scale(scale_uv=True, shear=True),
        "아일랜드 스케일 정규화",
    )
    _require_finished(
        bpy.ops.uv.pack_islands(
            rotate=True,
            scale=True,
            margin_method="SCALED",
            margin=settings.island_margin,
        ),
        "UV 패킹",
    )
    return method


class _ContextState:
    """연산 중 바뀌는 Blender 모드와 선택 상태를 가능한 범위에서 복구한다."""

    def __init__(self, context, targets):
        self.context = context
        self.targets = tuple(targets)
        self.active_object = context.view_layer.objects.active
        self.active_mode = self.active_object.mode if self.active_object else "OBJECT"
        self.objects_in_mode = tuple(getattr(context, "objects_in_mode", ()))
        self.object_selection = tuple(
            (obj, obj.select_get()) for obj in context.view_layer.objects
        )
        self.mesh_select_mode = tuple(context.tool_settings.mesh_select_mode)
        self.use_uv_select_sync = context.tool_settings.use_uv_select_sync
        self.uv_select_mode = getattr(context.tool_settings, "uv_select_mode", None)
        self.mesh_states: dict[int, tuple[Any, dict[str, Any]]] = {}
        self.mesh_replacements: dict[int, Any] = {}
        self.prepared = False

    def prepare(self):
        self.prepared = True
        current = self.context.view_layer.objects.active
        if current is not None and current.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        for obj in self.targets:
            mesh = obj.data
            key = mesh.as_pointer()
            if key not in self.mesh_states:
                self.mesh_states[key] = (mesh, _capture_mesh_selection(mesh))
        self._deselect_all()

    def _deselect_all(self):
        for obj in self.context.view_layer.objects:
            try:
                if obj.select_get():
                    obj.select_set(False)
            except RuntimeError:
                pass

    def activate(self, obj):
        current = self.context.view_layer.objects.active
        if current is not None and current.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        self._deselect_all()
        obj.select_set(True)
        self.context.view_layer.objects.active = obj

    def register_mesh_replacement(self, source_mesh, replacement_mesh):
        self.mesh_replacements[source_mesh.as_pointer()] = replacement_mesh

    def clear_mesh_replacements(self):
        self.mesh_replacements.clear()

    def restore(self):
        if not self.prepared:
            return

        current = self.context.view_layer.objects.active
        if current is not None and current.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        self.context.tool_settings.mesh_select_mode = self.mesh_select_mode
        self.context.tool_settings.use_uv_select_sync = self.use_uv_select_sync
        if self.uv_select_mode is not None:
            self.context.tool_settings.uv_select_mode = self.uv_select_mode

        for key, (source_mesh, snapshot) in self.mesh_states.items():
            _restore_mesh_selection(source_mesh, snapshot)
            replacement = self.mesh_replacements.get(key)
            if replacement is not None and replacement != source_mesh:
                _restore_mesh_selection(replacement, snapshot)

        self._deselect_all()
        for obj, selected in self.object_selection:
            try:
                obj.select_set(selected)
            except (ReferenceError, RuntimeError):
                pass
        try:
            self.context.view_layer.objects.active = self.active_object
        except (ReferenceError, RuntimeError):
            self.context.view_layer.objects.active = None

        if self.active_mode != "OBJECT" and self.active_object is not None:
            bpy.ops.object.mode_set(mode=self.active_mode)
        self.prepared = False


def _remove_scratch_object(obj, remove_mesh):
    mesh = None
    if obj is not None:
        try:
            mesh = obj.data
        except (ReferenceError, RuntimeError):
            pass
        try:
            if bpy.context.view_layer.objects.active == obj:
                bpy.context.view_layer.objects.active = None
        except (ReferenceError, RuntimeError):
            pass
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass
    try:
        should_remove_mesh = remove_mesh and mesh is not None and mesh.users == 0
    except (ReferenceError, RuntimeError):
        should_remove_mesh = False
    if should_remove_mesh:
        try:
            bpy.data.meshes.remove(mesh)
        except (ReferenceError, RuntimeError):
            pass


def _cleanup_atlas_proxies(objects=()):
    """알려진 proxy와 예약 표식이 남은 proxy를 두 번째 best-effort로 정리한다."""

    candidates = {}
    for obj in objects:
        try:
            candidates[obj.as_pointer()] = obj
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    for obj in tuple(bpy.data.objects):
        try:
            if bool(obj.get(ATLAS_PROXY_PROPERTY, False)):
                candidates[obj.as_pointer()] = obj
        except (ReferenceError, RuntimeError):
            pass
    for obj in tuple(candidates.values()):
        _remove_scratch_object(obj, remove_mesh=False)


def _remove_orphan_mesh(mesh):
    if mesh is not None and mesh.users == 0:
        try:
            bpy.data.meshes.remove(mesh)
        except (ReferenceError, RuntimeError):
            pass


def _quality_key(quality, analysis, candidate_index):
    defects = (
        int(getattr(quality, "overlap_pairs", 0))
        + int(getattr(quality, "degenerate_triangles", 0))
        + int(getattr(quality, "flipped_triangles", 0))
    )
    objective = float(getattr(quality, "objective_score", 0.0))
    if not isfinite(objective):
        objective = 0.0
    distortion = float(getattr(quality, "area_distortion_p95", float("inf")))
    if not isfinite(distortion):
        distortion = float("inf")
    return (
        int(bool(getattr(quality, "valid", False))),
        -defects,
        -int(getattr(quality, "overlap_pairs", 0)),
        objective,
        -distortion,
        -len(analysis.seam_edges),
        -candidate_index,
    )


def _evaluate_candidate(context, state, group, analysis, settings, candidate_index):
    scratch_mesh = group.source_mesh.copy()
    scratch_mesh.name = f"{group.source_mesh.name}.UVMappingCandidate"
    scratch_object = bpy.data.objects.new(
        f"_UVMappingCandidate_{candidate_index}", scratch_mesh
    )
    context.scene.collection.objects.link(scratch_object)
    scratch_object.hide_render = True

    try:
        state.activate(scratch_object)
        _write_seams(
            scratch_mesh,
            analysis.seam_edges,
            preserve=settings.seam_policy == "PRESERVE",
        )
        layer = _select_uv_layer(scratch_mesh, settings)
        layer_name = layer.name

        bpy.ops.object.mode_set(mode="EDIT")
        context.tool_settings.use_uv_select_sync = True
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        method = _run_uv_pipeline(settings)
        bpy.ops.object.mode_set(mode="OBJECT")

        quality = evaluate_uv_quality(
            scratch_mesh,
            uv_layer_name=layer_name,
            seam_count=len(analysis.seam_edges),
            chart_count=getattr(analysis, "chart_count", 0),
        )
        # 원본의 미리보기 속성이 scratch에 복제됐더라도 결과 datablock에는 남기지 않는다.
        _clear_preview(scratch_mesh)
        artifact = _CandidateArtifact(
            group=group,
            mesh=scratch_mesh,
            analysis=analysis,
            quality=quality,
            uv_layer_name=layer_name,
            method=method,
            candidate_index=candidate_index,
        )
        _remove_scratch_object(scratch_object, remove_mesh=False)
        return artifact
    except Exception:
        try:
            if scratch_object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except (ReferenceError, RuntimeError):
            pass
        _remove_scratch_object(scratch_object, remove_mesh=True)
        raise


def _evaluate_group(context, state, group, settings):
    options = _build_analysis_options(settings)
    candidates = generate_analysis_candidates(
        group.source_mesh,
        options,
        settings.quality_level,
    )
    if not candidates:
        raise RuntimeError(f"{group.source_mesh.name}: Seam 후보가 없습니다.")

    winner = None
    winner_key = None
    failures = []
    for candidate_index, analysis in enumerate(candidates):
        try:
            artifact = _evaluate_candidate(
                context,
                state,
                group,
                analysis,
                settings,
                candidate_index,
            )
        except Exception as exc:
            failures.append(f"후보 {candidate_index + 1}: {exc}")
            continue

        key = _quality_key(artifact.quality, artifact.analysis, candidate_index)
        if winner is None or key > winner_key:
            if winner is not None:
                _remove_orphan_mesh(winner.mesh)
            winner = artifact
            winner_key = key
        else:
            _remove_orphan_mesh(artifact.mesh)

    if winner is None:
        detail = failures[0] if failures else "알 수 없는 오류"
        raise RuntimeError(f"{group.source_mesh.name}: 모든 후보가 실패했습니다. {detail}")
    return winner


def _atlas_margin(settings):
    resolution = int(settings.texture_resolution)
    padding = int(settings.padding_pixels)
    if resolution <= 0:
        raise ValueError("텍스처 해상도는 0보다 커야 합니다.")
    if padding < 0:
        raise ValueError("패딩 픽셀은 0 이상이어야 합니다.")
    margin = padding / resolution
    if margin > 1.0:
        raise ValueError("패딩 픽셀은 텍스처 해상도보다 클 수 없습니다.")
    return resolution, padding, margin


def _pack_selected_artifacts(context, state, objects, margin):
    state.activate(objects[0])
    for obj in objects[1:]:
        obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    unique_meshes = tuple(getattr(context, "objects_in_mode_unique_data", ()))
    if len(unique_meshes) != len(objects):
        raise RuntimeError(
            "아틀라스 Edit Mode의 고유 Mesh 수가 임시 객체 수와 일치하지 않습니다."
        )
    context.tool_settings.use_uv_select_sync = True
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.select_all(action="SELECT")
    _require_finished(
        bpy.ops.uv.average_islands_scale(scale_uv=True, shear=True),
        "공유 아틀라스 스케일 정규화",
    )
    _require_finished(
        bpy.ops.uv.pack_islands(
            rotate=True,
            scale=True,
            margin_method="FRACTION",
            margin=margin,
        ),
        "공유 아틀라스 패킹",
    )
    bpy.ops.object.mode_set(mode="OBJECT")


def _pack_artifacts(
    context,
    state,
    artifacts,
    settings,
    fail_after_proxy_creation=False,
):
    """winner Mesh만 임시 객체로 연결해 최종 픽셀 기준 아틀라스를 패킹한다."""

    if not artifacts:
        return
    _, _, margin = _atlas_margin(settings)
    scratch_objects = []
    try:
        for index, artifact in enumerate(artifacts):
            layer = artifact.mesh.uv_layers.get(artifact.uv_layer_name)
            if layer is None:
                raise RuntimeError(
                    f"{artifact.mesh.name}: 최종 UV 레이어를 찾을 수 없습니다."
                )
            artifact.mesh.uv_layers.active_index = tuple(
                artifact.mesh.uv_layers
            ).index(layer)
            obj = bpy.data.objects.new(
                f"_UVMappingAtlas_{index}", artifact.mesh
            )
            scratch_objects.append(obj)
            context.scene.collection.objects.link(obj)
            obj.hide_render = True
            obj[ATLAS_PROXY_PROPERTY] = True

        if fail_after_proxy_creation:
            raise RuntimeError("테스트용 Atlas 패킹 실패가 주입되었습니다.")

        if settings.pack_shared_atlas:
            _pack_selected_artifacts(
                context, state, tuple(scratch_objects), margin
            )
        else:
            for obj in scratch_objects:
                _pack_selected_artifacts(context, state, (obj,), margin)

        for artifact in artifacts:
            artifact.quality = evaluate_uv_quality(
                artifact.mesh,
                uv_layer_name=artifact.uv_layer_name,
                seam_count=len(artifact.analysis.seam_edges),
                chart_count=getattr(artifact.analysis, "chart_count", 0),
            )

        for index, artifact in enumerate(artifacts):
            artifact.atlas_member_id = (
                f"mesh-{index:04d}:{artifact.group.source_mesh.name_full}"
            )
        if settings.pack_shared_atlas:
            report_groups = (tuple(artifacts),)
        else:
            report_groups = tuple((artifact,) for artifact in artifacts)

        for report_artifacts in report_groups:
            report = evaluate_atlas_quality(
                tuple(
                    (
                        artifact.atlas_member_id,
                        artifact.mesh,
                        artifact.uv_layer_name,
                    )
                    for artifact in report_artifacts
                ),
                max_pair_checks=1_000_000,
            )
            if report.out_of_bounds_count:
                raise RuntimeError(
                    "최종 Atlas UV가 0-1 범위를 벗어났습니다: "
                    f"{report.out_of_bounds_count}개"
                )
            if report.overlap_status == OVERLAP_EXACT and not report.valid:
                raise RuntimeError(
                    "최종 Atlas member 간 UV 겹침이 검출되었습니다: "
                    f"{report.overlap_pairs}쌍"
                )
            descriptors = tuple(
                {
                    "member_id": artifact.atlas_member_id,
                    "island_count": max(
                        0, int(getattr(artifact.analysis, "chart_count", 0))
                    ),
                    "mesh_name": artifact.group.source_mesh.name_full,
                    "object_names": tuple(
                        sorted(obj.name_full for obj in artifact.group.objects)
                    ),
                    "uv_layer_name": artifact.uv_layer_name,
                }
                for artifact in report_artifacts
            )
            atlas_context = build_atlas_context(
                report,
                descriptors,
                shared=bool(settings.pack_shared_atlas),
            )
            for artifact in report_artifacts:
                artifact.atlas_report = report
                artifact.atlas_context = atlas_context
    finally:
        current = context.view_layer.objects.active
        if current is not None and current.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                pass
        for obj in reversed(scratch_objects):
            _remove_scratch_object(obj, remove_mesh=False)
        _cleanup_atlas_proxies(scratch_objects)


def _json_text(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _capture_object_properties(obj):
    return {
        QUALITY_PROPERTY: obj[QUALITY_PROPERTY] if QUALITY_PROPERTY in obj else _MISSING,
        TEXTURE_JOB_PROPERTY: (
            obj[TEXTURE_JOB_PROPERTY] if TEXTURE_JOB_PROPERTY in obj else _MISSING
        ),
    }


def _restore_object_properties(obj, snapshot):
    for name, value in snapshot.items():
        if value is _MISSING:
            if name in obj:
                del obj[name]
        else:
            obj[name] = value


def _prepare_payloads(artifacts, settings):
    resolution, padding, packing_margin = _atlas_margin(settings)
    target_resolution = (resolution, resolution)
    payloads = {}
    for artifact in artifacts:
        quality_json = _json_text(artifact.quality.to_dict())
        for obj in artifact.group.objects:
            texture_job_json = None
            if settings.generate_texture_job:
                texture_settings = {
                    "preset": settings.preset,
                    "quality_level": settings.quality_level,
                    "seam_policy": settings.seam_policy,
                    "unwrap_method": artifact.method,
                    "unwrap_iterations": settings.unwrap_iterations,
                    "packing_margin_method": "FRACTION",
                    "packing_margin_uv": packing_margin,
                    "pack_shared_atlas": bool(settings.pack_shared_atlas),
                    "generation_target": {
                        "resolution": target_resolution,
                        "padding_pixels": padding,
                        "udim_tiles": TEXTURE_TARGET_UDIMS,
                    },
                }
                job = build_texture_job(
                    artifact.mesh,
                    obj.name_full,
                    artifact.uv_layer_name,
                    artifact.quality,
                    artifact.analysis.seam_edges,
                    resolution=target_resolution,
                    padding=padding,
                    udim_tiles=TEXTURE_TARGET_UDIMS,
                    object_transform=tuple(tuple(row) for row in obj.matrix_world),
                    coordinate_space="OBJECT",
                    settings=texture_settings,
                    packing_margin_method="FRACTION",
                    packing_margin_uv=packing_margin,
                    pack_shared_atlas=bool(settings.pack_shared_atlas),
                    atlas_context=artifact.atlas_context,
                    atlas_member_id=artifact.atlas_member_id,
                )
                job_payload = job.to_dict()
                job_payload["settings"] = texture_settings
                texture_job_json = _json_text(job_payload)
            payloads[obj.as_pointer()] = (quality_json, texture_job_json)
    return payloads


def _commit_artifacts(
    artifacts,
    payloads,
    state,
    snapshots,
    fail_after_bindings=0,
):
    binding_count = 0
    for artifact in artifacts:
        state.register_mesh_replacement(artifact.group.source_mesh, artifact.mesh)
        artifact.mesh.name = f"{artifact.group.source_mesh.name}.UVMappingResult"
        for obj in artifact.group.objects:
            snapshots.append((obj, obj.data, _capture_object_properties(obj)))
            obj.data = artifact.mesh
            binding_count += 1
            if fail_after_bindings and binding_count >= fail_after_bindings:
                raise RuntimeError("테스트용 commit 실패가 주입되었습니다.")

    for artifact in artifacts:
        for obj in artifact.group.objects:
            quality_json, texture_job_json = payloads[obj.as_pointer()]
            obj[QUALITY_PROPERTY] = quality_json
            if texture_job_json is None:
                if TEXTURE_JOB_PROPERTY in obj:
                    del obj[TEXTURE_JOB_PROPERTY]
            else:
                obj[TEXTURE_JOB_PROPERTY] = texture_job_json


def _rollback_commit(snapshots, state):
    for obj, original_mesh, properties in reversed(snapshots):
        try:
            obj.data = original_mesh
            _restore_object_properties(obj, properties)
        except (ReferenceError, RuntimeError):
            pass
    state.clear_mesh_replacements()


def _preview_attribute(mesh):
    attribute = mesh.attributes.get(PREVIEW_ATTRIBUTE)
    if attribute is not None and (
        attribute.domain != "EDGE" or attribute.data_type != "FLOAT"
    ):
        mesh.attributes.remove(attribute)
        attribute = None
    if attribute is None:
        attribute = mesh.attributes.new(PREVIEW_ATTRIBUTE, "FLOAT", "EDGE")
    return attribute


def _write_preview(mesh, seam_edges):
    seam_indices = set(seam_edges)
    attribute = _preview_attribute(mesh)
    attribute.data.foreach_set(
        "value",
        [1.0 if edge.index in seam_indices else 0.0 for edge in mesh.edges],
    )
    mesh.update()


def _clear_preview(mesh):
    attribute = mesh.attributes.get(PREVIEW_ATTRIBUTE)
    if attribute is not None:
        mesh.attributes.remove(attribute)
        mesh.update()


def _finalize_success(artifacts):
    """commit 이후의 이름·고아 정리는 실패해도 연산 성공을 되돌리지 않는다."""

    for artifact in artifacts:
        source_mesh = artifact.group.source_mesh
        source_name = None
        try:
            source_name = source_mesh.name
            _clear_preview(source_mesh)
        except (ReferenceError, RuntimeError):
            pass
        try:
            _remove_orphan_mesh(source_mesh)
        except (ReferenceError, RuntimeError):
            pass
        if source_name is not None:
            try:
                artifact.mesh.name = source_name
            except (ReferenceError, RuntimeError):
                pass


def clear_preview_attributes(meshes=None):
    """등록 해제와 Clear 연산자에서 reserved 미리보기 속성을 정리한다."""

    for mesh in tuple(meshes) if meshes is not None else tuple(bpy.data.meshes):
        try:
            _clear_preview(mesh)
        except (ReferenceError, RuntimeError):
            pass


class UVMAPPING_OT_analyze(Operator):
    """선택 메시를 변경하지 않고 Seam 후보를 분석합니다."""

    bl_idname = "uvmapping.analyze"
    bl_label = "Seam 분석"
    bl_description = "선택 메시를 분석하고 예상 Seam 수를 표시합니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "uvmapping_settings", None)
        return settings is not None and bool(_target_objects(context, settings))

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        targets = _target_objects(context, settings)
        groups = _group_targets(targets)
        state = _ContextState(context, targets)
        try:
            state.prepare()
            seam_count = 0
            chart_count = 0
            for group in groups:
                result = analyze_mesh(
                    group.source_mesh,
                    _build_analysis_options(settings),
                )
                seam_count += len(result.seam_edges)
                chart_count += getattr(result, "chart_count", 0)
            message = (
                f"객체 {len(targets)}개/메시 {len(groups)}개: "
                f"Seam {seam_count}개, 예상 차트 {chart_count}개"
            )
            settings.last_result = message
            self.report({"INFO"}, message)
            state.restore()
            return {"FINISHED"}
        except Exception as exc:
            try:
                state.restore()
            except Exception:
                pass
            settings.last_result = f"분석 실패: {exc}"
            self.report({"ERROR"}, settings.last_result)
            return {"CANCELLED"}


class UVMAPPING_OT_preview_seams(Operator):
    """실제 Seam을 바꾸지 않고 EDGE 속성에 기본 후보를 기록합니다."""

    bl_idname = "uvmapping.preview_seams"
    bl_label = "Seam 미리보기"
    bl_description = "실제 Seam을 건드리지 않고 예약 EDGE 속성에 후보를 기록합니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "uvmapping_settings", None)
        return settings is not None and bool(_target_objects(context, settings))

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        targets = _target_objects(context, settings)
        groups = _group_targets(targets)
        state = _ContextState(context, targets)
        seam_snapshot = {
            group.source_mesh.as_pointer(): _capture_seams(group.source_mesh)
            for group in groups
        }
        try:
            state.prepare()
            total = 0
            for group in groups:
                candidates = generate_analysis_candidates(
                    group.source_mesh,
                    _build_analysis_options(settings),
                    settings.quality_level,
                )
                if not candidates:
                    raise RuntimeError(f"{group.source_mesh.name}: Seam 후보가 없습니다.")
                _write_preview(group.source_mesh, candidates[0].seam_edges)
                total += len(candidates[0].seam_edges)
                if _capture_seams(group.source_mesh) != seam_snapshot[group.source_mesh.as_pointer()]:
                    raise RuntimeError("미리보기가 실제 Seam을 변경했습니다.")
            state.restore()
            message = f"메시 {len(groups)}개에 Seam 후보 {total}개를 미리보기 속성으로 기록했습니다."
            settings.last_result = message
            self.report({"INFO"}, message)
            return {"FINISHED"}
        except Exception as exc:
            clear_preview_attributes(group.source_mesh for group in groups)
            try:
                state.restore()
            except Exception:
                pass
            settings.last_result = f"미리보기 실패: {exc}"
            self.report({"ERROR"}, settings.last_result)
            return {"CANCELLED"}


class UVMAPPING_OT_clear_preview(Operator):
    """예약된 Seam 미리보기 EDGE 속성을 제거합니다."""

    bl_idname = "uvmapping.clear_preview"
    bl_label = "미리보기 지우기"
    bl_description = "선택 메시에서 Seam 미리보기 속성을 제거합니다"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        targets = _target_objects(context, settings)
        meshes = {obj.data.as_pointer(): obj.data for obj in targets}
        if not meshes:
            meshes = {mesh.as_pointer(): mesh for mesh in bpy.data.meshes}
        clear_preview_attributes(meshes.values())
        settings.last_result = f"미리보기 속성 {len(meshes)}개 메시에서 정리"
        self.report({"INFO"}, settings.last_result)
        return {"FINISHED"}


class UVMAPPING_OT_auto_unwrap(Operator):
    """후보 평가부터 UV 패킹과 TextureJob 생성까지 한 번에 수행합니다."""

    bl_idname = "uvmapping.auto_unwrap"
    bl_label = "자동 UV 언랩"
    bl_description = "선택 메시의 후보를 비교해 최선의 UV를 원자적으로 적용합니다"
    bl_options = {"REGISTER", "UNDO"}

    debug_fail_after_bindings: IntProperty(
        name="테스트 commit 실패 위치",
        default=0,
        min=0,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    debug_fail_atlas_pack: IntProperty(
        name="테스트 Atlas 패킹 실패",
        default=0,
        min=0,
        max=1,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "uvmapping_settings", None)
        return settings is not None and bool(_target_objects(context, settings))

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        targets = _target_objects(context, settings)
        groups = _group_targets(targets)
        state = _ContextState(context, targets)
        artifacts = []
        commit_snapshots = []

        try:
            state.prepare()
            for group in groups:
                artifacts.append(_evaluate_group(context, state, group, settings))

            _pack_artifacts(
                context,
                state,
                artifacts,
                settings,
                fail_after_proxy_creation=bool(self.debug_fail_atlas_pack),
            )
            _cleanup_atlas_proxies()
            payloads = _prepare_payloads(artifacts, settings)
            _commit_artifacts(
                artifacts,
                payloads,
                state,
                commit_snapshots,
                fail_after_bindings=self.debug_fail_after_bindings,
            )
            state.restore()
            _cleanup_atlas_proxies()
        except Exception as exc:
            _cleanup_atlas_proxies()
            _rollback_commit(commit_snapshots, state)
            for artifact in artifacts:
                _remove_orphan_mesh(artifact.mesh)
            try:
                state.restore()
            except Exception as restore_exc:
                self.report({"ERROR"}, f"상태 복구 실패: {restore_exc}")
            _cleanup_atlas_proxies()
            settings.last_result = f"자동 언랩 실패: {exc}"
            self.report({"ERROR"}, settings.last_result)
            return {"CANCELLED"}

        # 여기부터는 원본이 이미 제거될 수 있으므로 transaction rollback 범위 밖이다.
        _finalize_success(artifacts)
        seam_count = sum(len(artifact.analysis.seam_edges) for artifact in artifacts)
        valid_count = sum(bool(artifact.quality.valid) for artifact in artifacts)
        partial_count = sum(group.has_unselected_users for group in groups)
        atlas_contexts = {
            artifact.atlas_context["atlas_id"]: artifact.atlas_context
            for artifact in artifacts
            if artifact.atlas_context is not None
        }
        unverified_atlas_count = sum(
            context["overlap_status"] == OVERLAP_BUDGET_EXCEEDED
            for context in atlas_contexts.values()
        )
        message = (
            f"객체 {len(targets)}개/메시 {len(groups)}개: Seam {seam_count}개, "
            f"품질 통과 {valid_count}/{len(groups)}, 부분 공유 분리 {partial_count}개, "
            f"Atlas 미검증 {unverified_atlas_count}개"
        )
        settings.last_result = message
        self.report({"INFO"}, message)
        return {"FINISHED"}


classes = (
    UVMAPPING_OT_analyze,
    UVMAPPING_OT_preview_seams,
    UVMAPPING_OT_clear_preview,
    UVMAPPING_OT_auto_unwrap,
)


__all__ = (
    "PREVIEW_ATTRIBUTE",
    "QUALITY_PROPERTY",
    "TEXTURE_JOB_PROPERTY",
    "classes",
    "clear_preview_attributes",
    "UVMAPPING_OT_analyze",
    "UVMAPPING_OT_preview_seams",
    "UVMAPPING_OT_clear_preview",
    "UVMAPPING_OT_auto_unwrap",
)
