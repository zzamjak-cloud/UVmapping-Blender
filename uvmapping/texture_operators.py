"""참조 분석과 단일 3면도 생성을 Blender에 연결하는 연산자."""

from __future__ import annotations

from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import bpy
from bpy.props import CollectionProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector

from . import clipboard_image, native_input, texture_bake
from .properties import get_addon_preferences
from .quality import evaluate_atlas_quality
from .openrouter_provider import validate_model_slug
from .texture_job import TEXTURE_JOB_PROPERTY, ensure_texture_jobs
from .texture_pipeline import (
    build_reference_analysis_prompt,
    compile_turnaround_prompt,
    parse_reference_analysis,
    validate_reference_image_path,
)

# 3면도 생성 요청과 같은 21:9 캔버스로 contact sheet를 만들어 좌표계를 일치시킨다.
CONTACT_SHEET_ASPECT = 21.0 / 9.0
# 형상 가이드는 AI가 실루엣을 따라 그릴 수 있을 만큼 선명해야 한다.
MODEL_CAPTURE_RESOLUTION = 1024
# 투명 배경은 Provider마다 다르게 합성된다. 불투명 흰 배경 위의 회색 모델이
# 실루엣 대비가 가장 분명하다. 외곽선까지 그리면 가이드가 "일러스트 대상"처럼
# 보여 이미지 모델이 실루엣을 따라 그리는 대신 다시 그리기 시작한다.
CAPTURE_BACKGROUND = (1.0, 1.0, 1.0)
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_REFERENCE_IMAGES = 5
TEXTURE_DESIGN_STATE_PROPERTY = "uvmapping_texture_design_state"
_ACTIVE_PROCESSES: set[subprocess.Popen] = set()
_ACTIVE_JOB_DIRS: set[Path] = set()
# Blender는 execute()가 반환하는 즉시 Operator RNA를 해제하므로, 진행 중인
# AI 작업 상태는 반드시 연산자 밖(모듈 레지스트리)에 보관해야 한다.
_ACTIVE_RUNS: list = []
_ACTIVE_TIMERS: set = set()


def _absolute_path(path: str) -> Path:
    return Path(bpy.path.abspath(path)).expanduser().resolve()


def _reference_paths(settings, *, allow_empty: bool = False) -> tuple[Path, ...]:
    raw_paths = tuple(
        Path(bpy.path.abspath(item.path)).expanduser()
        for item in settings.reference_images
    )
    if not raw_paths:
        if allow_empty:
            return ()
        raise ValueError("참조 이미지를 한 장 이상 추가해 주세요.")
    if len(raw_paths) > MAX_REFERENCE_IMAGES:
        raise ValueError(f"참조 이미지는 최대 {MAX_REFERENCE_IMAGES}장까지 사용할 수 있습니다.")
    for path in raw_paths:
        validate_reference_image_path(path)
    return tuple(path.resolve() for path in raw_paths)


def _reference_digest(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_api_key(context) -> str:
    """환경설정에 저장된 OpenRouter 키를 읽고 없으면 환경 변수로 대체한다."""

    preferences = get_addon_preferences(context)
    api_key = (
        getattr(preferences, "openrouter_api_key", "").strip()
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
    )
    if not api_key:
        raise ValueError(
            "Blender 애드온 환경설정에 OpenRouter API 키를 입력해 주세요."
        )
    return api_key


def _resolved_models(settings) -> tuple[str, str]:
    """UI에서 고른 (분석 모델, 이미지 모델) OpenRouter 식별자를 검증해 반환한다."""

    analysis_model = validate_model_slug(settings.texture_analysis_model)
    image_model = validate_model_slug(settings.texture_image_model)
    return analysis_model, image_model


def _selected_meshes(context) -> tuple:
    candidates = getattr(context, "selected_editable_objects", ())
    return tuple(
        obj
        for obj in candidates
        if obj.type == "MESH" and obj.data is not None and len(obj.data.polygons) > 0
    )


def registered_targets(context) -> tuple:
    """패널에 명시 등록한 대상 객체를 순서대로, 중복 없이 돌려준다."""

    settings = getattr(context.scene, "uvmapping_settings", None)
    if settings is None:
        return ()
    resolved = {}
    for item in settings.target_objects:
        obj = item.object
        if obj is None or obj.type != "MESH" or obj.data is None:
            continue
        if not len(obj.data.polygons):
            continue
        resolved.setdefault(obj.name, obj)
    return tuple(resolved.values())


def texture_targets(context) -> tuple:
    """등록 목록이 있으면 그것을, 없으면 현재 선택을 대상으로 삼는다."""

    return registered_targets(context) or _selected_meshes(context)


def non_object_mode_names(context) -> tuple[str, ...]:
    """Edit·Paint 모드에서는 원본 Mesh가 최신이 아니므로 대상에서 막는다."""

    return tuple(obj.name for obj in texture_targets(context) if obj.mode != "OBJECT")


def ensure_object_mode(context, objects: tuple) -> None:
    """대상이 Edit·Paint 모드면 Object Mode로 되돌린 뒤 진행한다."""

    if all(obj.mode == "OBJECT" for obj in objects):
        return
    active = getattr(context, "object", None)
    if active is not None and active.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None:
        previous_active = view_layer.objects.active
        for obj in objects:
            if obj.mode == "OBJECT":
                continue
            try:
                view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode="OBJECT")
            except (RuntimeError, ReferenceError):
                pass
        if previous_active is not None and previous_active.name in bpy.data.objects:
            view_layer.objects.active = previous_active
    remaining = tuple(obj.name for obj in objects if obj.mode != "OBJECT")
    if remaining:
        raise ValueError(
            f"{', '.join(remaining[:3])}: Object Mode로 전환하지 못했습니다. "
            "직접 Object Mode로 바꾼 뒤 다시 실행해 주세요."
        )


def _validated_texture_targets(context, objects=None) -> tuple:
    """선택 객체의 현재 UV에서 TextureJob 계약을 새로 계산해 기록한다.

    계약은 UV·메시 내용만으로 결정되므로, 3면도 생성 시점과 베이크 시점에 각각
    다시 계산해 비교하면 그 사이에 UV가 바뀌었는지 확인할 수 있다.
    """

    objects = tuple(objects) if objects else texture_targets(context)
    if not objects:
        raise ValueError("대상 객체를 등록하거나 Mesh 객체를 선택해 주세요.")
    ensure_object_mode(context, objects)
    ensure_texture_jobs(objects, context.scene.uvmapping_settings)
    return objects


def _nearly_equal_values(left, right, tolerance: float = 1.0e-6) -> bool:
    """중첩된 숫자 목록을 작은 부동소수 오차 안에서 비교한다."""

    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _nearly_equal_values(a, b, tolerance) for a, b in zip(left, right)
        )
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return left == right


def _validated_bake_targets(context, objects=None) -> tuple[tuple, dict, tuple[dict, ...]]:
    """현재 대상·UV·Transform이 3면도 생성 시점과 같은지 검사한다."""

    objects = _validated_texture_targets(context, objects)
    states = []
    jobs = []
    for obj in objects:
        raw_state = obj.get(TEXTURE_DESIGN_STATE_PROPERTY)
        if not raw_state:
            raise ValueError(f"{obj.name}: 먼저 단일 3면도를 생성해 주세요.")
        try:
            state = json.loads(raw_state)
            job = json.loads(obj[TEXTURE_JOB_PROPERTY])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{obj.name}: 텍스처 상태 정보가 올바르지 않습니다.") from exc
        if state.get("status") not in {"TURNAROUND_READY", "ALBEDO_APPLIED"}:
            raise ValueError(f"{obj.name}: 적용 가능한 3면도 상태가 아닙니다.")
        states.append(state)
        jobs.append(job)

    first = states[0]
    signature = (
        first.get("turnaround_sha256"),
        json.dumps(first.get("views", {}), sort_keys=True),
    )
    if any(
        (
            state.get("turnaround_sha256"),
            json.dumps(state.get("views", {}), sort_keys=True),
        )
        != signature
        for state in states[1:]
    ):
        raise ValueError("선택 객체들이 서로 다른 3면도 디자인을 사용하고 있습니다.")

    target_names = tuple(first.get("target_objects", ()))
    if set(target_names) != {obj.name for obj in objects}:
        raise ValueError("3면도를 생성했던 Mesh 객체를 모두 다시 대상으로 지정해 주세요.")
    projection = first.get("projection")
    if not isinstance(projection, dict):
        raise ValueError("구버전 3면도입니다. 정확한 투영을 위해 다시 생성해 주세요.")
    current_projection = _projection_contract(context, objects)
    for key in ("bounds_min", "bounds_max", "center", "ortho_scale"):
        if not _nearly_equal_values(projection.get(key), current_projection.get(key)):
            raise ValueError("모델 위치 또는 형상이 변경되었습니다. 3면도를 다시 생성해 주세요.")
    saved_geometry_hash = str(projection.get("evaluated_geometry_sha256", ""))
    if not saved_geometry_hash:
        raise ValueError("구버전 3면도입니다. 정확한 투영을 위해 다시 생성해 주세요.")
    if saved_geometry_hash != current_projection["evaluated_geometry_sha256"]:
        raise ValueError("모델 형상 또는 Modifier 결과가 변경되었습니다. 3면도를 다시 생성해 주세요.")
    saved_transforms = projection.get("object_transforms", {})
    for obj in objects:
        if not _nearly_equal_values(
            saved_transforms.get(obj.name), current_projection["object_transforms"][obj.name]
        ):
            raise ValueError(f"{obj.name}: Transform이 변경되어 3면도를 다시 생성해야 합니다.")

    source_jobs = {
        str(item.get("object_name", "")): item
        for item in first.get("source_jobs", ())
        if isinstance(item, dict)
    }
    compared_keys = (
        "atlas_id",
        "atlas_hash",
        "atlas_member_id",
        "uv_hash",
        "mesh_hash",
        "uv_layer_name",
    )
    for obj, job in zip(objects, jobs):
        source = source_jobs.get(obj.name)
        if source is None or any(
            source.get(key, "") != job.get(key, "") for key in compared_keys
        ):
            raise ValueError(f"{obj.name}: UV 또는 메시가 변경되어 3면도를 다시 생성해야 합니다.")

    if all(job.get("atlas_member_id") and job.get("uv_hash") for job in jobs):
        atlas_report = evaluate_atlas_quality(
            [
                (str(job["atlas_member_id"]), obj.data, str(job["uv_layer_name"]))
                for obj, job in zip(objects, jobs)
            ]
        )
        for obj, job in zip(objects, jobs):
            current_hash = atlas_report.member_uv_hashes.get(str(job["atlas_member_id"]))
            if current_hash != job.get("uv_hash"):
                raise ValueError(f"{obj.name}: UV가 변경되어 3면도를 다시 생성해야 합니다.")
        if atlas_report.atlas_hash != jobs[0].get("atlas_hash"):
            raise ValueError("공유 UV Atlas 배치가 변경되어 3면도를 다시 생성해야 합니다.")

    view_paths = first.get("views", {})
    expected_hashes = first.get("view_sha256", {})
    for name in ("front", "right", "back"):
        path = Path(str(view_paths.get(name, "")))
        if not path.is_file():
            raise ValueError(f"{name.upper()} 3면도 파일을 찾을 수 없습니다.")
        expected = str(expected_hashes.get(name, ""))
        if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"{name.upper()} 3면도 파일이 생성 뒤 변경되었습니다.")
    return objects, first, tuple(jobs)


def can_bake_diffuse(context) -> bool:
    """패널 draw에서 파일 해시 계산 없이 베이크 가능성을 빠르게 표시한다."""

    objects = texture_targets(context)
    return (
        bool(objects)
        and not _ACTIVE_RUNS
        and all(obj.get(TEXTURE_DESIGN_STATE_PROPERTY) for obj in objects)
    )


def _world_bounds(context, objects: tuple) -> tuple[Vector, Vector]:
    depsgraph = context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        points.extend(
            evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box
        )
    minimum = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    return minimum, maximum


def _evaluated_geometry_sha256(context, objects: tuple) -> str:
    """캡처에 사용한 평가 Mesh 위치와 노멀의 결정적 해시를 만든다."""

    depsgraph = context.evaluated_depsgraph_get()
    digest = hashlib.sha256()
    for obj in sorted(objects, key=lambda item: item.name):
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            matrix = evaluated.matrix_world
            normal_matrix = matrix.to_3x3().inverted_safe().transposed()
            digest.update(obj.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(
                f"{len(mesh.vertices)}:{len(mesh.edges)}:{len(mesh.polygons)}".encode("ascii")
            )
            digest.update(b"\0")
            for vertex in mesh.vertices:
                position = matrix @ vertex.co
                normal = (normal_matrix @ vertex.normal).normalized()
                for value in (*position, *normal):
                    digest.update(float(value).hex().encode("ascii"))
                    digest.update(b"\0")
        finally:
            evaluated.to_mesh_clear()
    return digest.hexdigest()


def _projection_contract(context, objects: tuple) -> dict:
    """모델 캡처와 UV 투영이 공유하는 직교 카메라 계약을 만든다."""

    minimum, maximum = _world_bounds(context, objects)
    center = (minimum + maximum) * 0.5
    extent = maximum - minimum
    scale = max(extent.x, extent.y, extent.z, 0.01) * 1.2
    distance = max(extent.length, 1.0) * 2.0
    return {
        "bounds_min": tuple(minimum),
        "bounds_max": tuple(maximum),
        "center": tuple(center),
        "extent": tuple(extent),
        "ortho_scale": float(scale),
        "camera_distance": float(distance),
        "evaluated_geometry_sha256": _evaluated_geometry_sha256(context, objects),
        "object_transforms": {
            obj.name: tuple(tuple(float(value) for value in row) for row in obj.matrix_world)
            for obj in objects
        },
    }


def _render_model_views(
    context, objects: tuple, projection: dict | None = None
) -> tuple[Path, ...]:
    """선택 모델을 임시 직교 카메라로 FRONT/RIGHT/BACK 순서로 렌더링한다."""

    scene = context.scene
    projection = projection or _projection_contract(context, objects)
    center = Vector(projection["center"])
    extent = Vector(projection["extent"])
    scale = float(projection["ortho_scale"])
    distance = float(projection["camera_distance"])
    output_dir = Path(tempfile.gettempdir()) / "uvmapping_ai" / uuid.uuid4().hex
    output_dir.mkdir(parents=True, exist_ok=False)

    camera_data = bpy.data.cameras.new("UVMapping AI 임시 카메라")
    camera = bpy.data.objects.new("UVMapping AI 임시 카메라", camera_data)
    scene.collection.objects.link(camera)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = scale
    camera_data.clip_start = max(scale * 1.0e-5, 1.0e-4)
    camera_data.clip_end = max(1000.0, distance + extent.length * 3.0)

    render = scene.render
    shading = scene.display.shading
    view_settings = scene.view_settings
    saved = {
        "view_transform": view_settings.view_transform,
        "view_look": view_settings.look,
        "view_exposure": view_settings.exposure,
        "view_gamma": view_settings.gamma,
        "camera": scene.camera,
        "engine": render.engine,
        "filepath": render.filepath,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "film_transparent": render.film_transparent,
        "file_format": render.image_settings.file_format,
        "shading_light": shading.light,
        "shading_color_type": shading.color_type,
        "shading_single_color": tuple(shading.single_color),
        "shading_background_type": shading.background_type,
        "shading_background_color": tuple(shading.background_color),
    }
    hidden = {obj: obj.hide_render for obj in scene.objects if obj != camera}
    selected = set(objects)
    paths = []
    try:
        for obj in hidden:
            obj.hide_render = obj not in selected
        try:
            render.engine = "BLENDER_WORKBENCH_NEXT"
        except (TypeError, ValueError):
            render.engine = "BLENDER_WORKBENCH"
        # AgX 같은 뷰 트랜스폼은 흰 배경을 0.77 회색으로 눌러 실루엣 대비와
        # 외곽선을 흐린다. 형상 가이드는 색 변환 없이 그대로 저장한다.
        view_settings.view_transform = "Standard"
        view_settings.look = "None"
        view_settings.exposure = 0.0
        view_settings.gamma = 1.0
        shading.light = "STUDIO"
        shading.color_type = "SINGLE"
        shading.single_color = (0.55, 0.55, 0.55)
        shading.background_type = "VIEWPORT"
        shading.background_color = CAPTURE_BACKGROUND
        scene.camera = camera
        render.resolution_x = MODEL_CAPTURE_RESOLUTION
        render.resolution_y = MODEL_CAPTURE_RESOLUTION
        render.resolution_percentage = 100
        render.film_transparent = False
        render.image_settings.file_format = "PNG"

        views = (
            ("front", Vector((0.0, -distance, 0.0))),
            ("right", Vector((distance, 0.0, 0.0))),
            ("back", Vector((0.0, distance, 0.0))),
        )
        for name, offset in views:
            camera.location = center + offset
            camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
            path = output_dir / f"model_{name}.png"
            render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
            paths.append(path)
    finally:
        for obj, hide_render in hidden.items():
            obj.hide_render = hide_render
        scene.camera = saved["camera"]
        render.engine = saved["engine"]
        render.filepath = saved["filepath"]
        render.resolution_x = saved["resolution_x"]
        render.resolution_y = saved["resolution_y"]
        render.resolution_percentage = saved["resolution_percentage"]
        render.film_transparent = saved["film_transparent"]
        render.image_settings.file_format = saved["file_format"]
        view_settings.view_transform = saved["view_transform"]
        view_settings.look = saved["view_look"]
        view_settings.exposure = saved["view_exposure"]
        view_settings.gamma = saved["view_gamma"]
        shading.light = saved["shading_light"]
        shading.color_type = saved["shading_color_type"]
        shading.single_color = saved["shading_single_color"]
        shading.background_type = saved["shading_background_type"]
        shading.background_color = saved["shading_background_color"]
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)
    return tuple(paths)


def _join_horizontal(
    paths: tuple[Path, ...],
    output_path: Path,
    aspect_ratio: float = CONTACT_SHEET_ASPECT,
    background: tuple[float, float, float] = CAPTURE_BACKGROUND,
) -> Path:
    """모델 뷰를 가로로 잇고, AI 캔버스와 같은 종횡비가 되도록 위아래를 채운다.

    생성 요청은 21:9 캔버스를 3열로 나누라고 지시하므로, 참조로 주는 contact
    sheet도 같은 종횡비여야 AI가 각 열의 정사각형 viewport 위치를 그대로 따라
    그린다. 그래야 베이크 때 모델 투영 좌표와 생성 이미지 좌표가 어긋나지 않는다.
    """

    sources = [bpy.data.images.load(str(path), check_existing=False) for path in paths]
    target = None
    try:
        width, height = sources[0].size
        if any(tuple(image.size) != (width, height) for image in sources):
            raise RuntimeError("모델 뷰 렌더 크기가 서로 다릅니다.")
        channels = 4
        target_width = width * len(sources)
        target_height = max(height, int(round(target_width / max(1.0e-6, aspect_ratio))))
        row_offset = (target_height - height) // 2
        pixels = array("f", [*background, 1.0][:channels]) * (target_width * target_height)
        for column, source in enumerate(sources):
            source_pixels = array("f", [0.0]) * (width * height * channels)
            source.pixels.foreach_get(source_pixels)
            for row in range(height):
                source_start = row * width * channels
                target_start = (
                    (row + row_offset) * target_width + column * width
                ) * channels
                pixels[target_start : target_start + width * channels] = source_pixels[
                    source_start : source_start + width * channels
                ]
        target = bpy.data.images.new(
            "UVMapping Model Contact Sheet",
            width=target_width,
            height=target_height,
            alpha=True,
        )
        target.pixels.foreach_set(pixels)
        target.filepath_raw = str(output_path)
        target.file_format = "PNG"
        target.save()
        return output_path
    finally:
        if target is not None:
            bpy.data.images.remove(target)
        for source in sources:
            bpy.data.images.remove(source)


def _crop_turnaround(path: Path) -> tuple[Path, Path, Path]:
    """AI 결과 한 장을 FRONT/RIGHT/BACK 같은 폭으로 로컬 분리한다."""

    source = bpy.data.images.load(str(path), check_existing=False)
    outputs = []
    try:
        width, height = source.size
        crop_width = width // 3
        if crop_width < 1:
            raise RuntimeError("생성된 3면도 이미지 폭이 너무 작습니다.")
        channels = 4
        source_pixels = array("f", [0.0]) * (width * height * channels)
        source.pixels.foreach_get(source_pixels)
        for column, view_name in enumerate(("front", "right", "back")):
            crop_pixels = array("f", [0.0]) * (crop_width * height * channels)
            for row in range(height):
                source_start = (row * width + column * crop_width) * channels
                crop_start = row * crop_width * channels
                crop_pixels[crop_start : crop_start + crop_width * channels] = source_pixels[
                    source_start : source_start + crop_width * channels
                ]
            crop = bpy.data.images.new(
                f"UVMapping {view_name.title()}",
                width=crop_width,
                height=height,
                alpha=True,
            )
            try:
                crop.pixels.foreach_set(crop_pixels)
                crop_path = path.with_name(f"{path.stem}_{view_name}.png")
                crop.filepath_raw = str(crop_path)
                crop.file_format = "PNG"
                crop.save()
                outputs.append(crop_path)
            finally:
                bpy.data.images.remove(crop)
    finally:
        bpy.data.images.remove(source)
    return tuple(outputs)


def _output_path(context, objects: tuple) -> Path:
    output_dir = _texture_output_directory()
    safe_name = bpy.path.clean_name(objects[0].name) if len(objects) == 1 else "selection"
    candidate = output_dir / f"{safe_name}_turnaround.png"
    index = 1
    while candidate.exists() or candidate.with_suffix(".jpg").exists():
        candidate = output_dir / f"{safe_name}_turnaround_{index:02d}.png"
        index += 1
    return candidate


def _texture_output_directory() -> Path:
    """현재 파일에 맞는 영구 또는 임시 AI 텍스처 폴더를 반환한다."""

    blend_path = Path(bpy.data.filepath) if bpy.data.filepath else None
    output_dir = (
        blend_path.parent / "textures"
        if blend_path
        else Path(tempfile.gettempdir()) / "uvmapping_ai" / "outputs"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _reference_output_directory() -> Path:
    """클립보드 참조를 결과 이미지와 구분해 보관한다."""

    output_dir = _texture_output_directory() / "references"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _tag_texture_panels_redraw() -> None:
    """타이머에서 바꾼 상태 문자열이 즉시 패널에 보이도록 다시 그린다."""

    window_manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(window_manager, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()


class _TextureRun:
    """작업자 프로세스를 Operator RNA 수명과 무관하게 감시하는 실행 단위."""

    def __init__(self, process, request_path, response_path, scene_pointer, payload, finish):
        self.process = process
        self.request_path = request_path
        self.response_path = response_path
        self.scene_pointer = scene_pointer
        self.payload = payload
        self.finish = finish
        self.timer = None

    def _scene(self):
        return next(
            (
                candidate
                for candidate in bpy.data.scenes
                if candidate.as_pointer() == self.scene_pointer
            ),
            None,
        )

    def poll_process(self):
        if self.process.poll() is None:
            return 0.2
        _ACTIVE_PROCESSES.discard(self.process)
        if self in _ACTIVE_RUNS:
            _ACTIVE_RUNS.remove(self)
        scene = self._scene()
        try:
            result = json.loads(self.response_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": f"AI 작업 결과를 읽지 못했습니다: {exc}"}
        self.cleanup_job_files()
        if not result.get("ok"):
            message = str(result.get("error", "알 수 없는 AI 작업 오류"))
            if scene is not None:
                scene.uvmapping_settings.texture_status = message
            _tag_texture_panels_redraw()
            return None
        if scene is None:
            _preserve_result_without_scene(result, self.payload)
            return None
        try:
            self.finish(scene, result, self.payload)
        except Exception as exc:  # noqa: BLE001 - 어떤 후처리 실패도 상태로 알린다.
            if result.get("output_path"):
                scene.uvmapping_settings.texture_output_path = str(result["output_path"])
                message = f"원본 AI 결과는 저장됐지만 후처리에 실패했습니다: {exc}"
            else:
                message = f"AI 결과 적용 실패: {exc}"
            scene.uvmapping_settings.texture_status = message
        _tag_texture_panels_redraw()
        return None

    def cleanup_job_files(self):
        for path in (self.request_path, self.response_path):
            if path is not None:
                path.unlink(missing_ok=True)
        if self.request_path is not None:
            _ACTIVE_JOB_DIRS.discard(self.request_path.parent)
            try:
                self.request_path.parent.rmdir()
            except OSError:
                pass


def _preserve_result_without_scene(result: dict, payload: dict) -> None:
    output_path = result.get("output_path")
    if not output_path:
        return
    state = json.dumps(
        {
            "schema_version": "1.0",
            "status": "RAW_RESULT_SCENE_REMOVED",
            "turnaround_path": str(output_path),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for name, session_uid in payload.get("target_keys", ()):
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.session_uid == session_uid:
            obj[TEXTURE_DESIGN_STATE_PROPERTY] = state


class _AsyncTextureMixin:
    """별도 Blender 프로세스를 앱 타이머로 감시하는 연산자 공통부."""

    def _start(self, context, job: dict, api_key: str, status: str, payload: dict, finish):
        if _ACTIVE_RUNS:
            self.report({"WARNING"}, "이미 AI 작업이 진행 중입니다.")
            return {"CANCELLED"}
        job_dir = Path(tempfile.gettempdir()) / "uvmapping_ai" / uuid.uuid4().hex
        job_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        _ACTIVE_JOB_DIRS.add(job_dir)
        request_path = job_dir / "request.json"
        response_path = job_dir / "response.json"
        descriptor = os.open(
            request_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(job, output, ensure_ascii=False)
        worker_path = Path(__file__).with_name("texture_worker.py")
        allowed_environment = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "TMPDIR",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
            "WINDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        }
        environment = {
            key: value for key, value in os.environ.items() if key in allowed_environment
        }
        process = None
        try:
            process = subprocess.Popen(
                (
                    bpy.app.binary_path,
                    "--background",
                    "--factory-startup",
                    "--python",
                    str(worker_path),
                    "--",
                    str(request_path),
                    str(response_path),
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            if process.stdin is None:
                raise OSError("AI 작업자 표준입력을 열지 못했습니다.")
            process.stdin.write((api_key + "\n").encode("utf-8"))
            process.stdin.close()
        except OSError as exc:
            if process is not None and process.poll() is None:
                process.terminate()
            for path in (request_path, response_path):
                path.unlink(missing_ok=True)
            _ACTIVE_JOB_DIRS.discard(job_dir)
            try:
                job_dir.rmdir()
            except OSError:
                pass
            message = f"AI 작업자 프로세스를 시작하지 못했습니다: {exc}"
            context.scene.uvmapping_settings.texture_status = message
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        _ACTIVE_PROCESSES.add(process)
        run = _TextureRun(
            process,
            request_path,
            response_path,
            context.scene.as_pointer(),
            payload,
            finish,
        )
        _ACTIVE_RUNS.append(run)
        context.scene.uvmapping_settings.texture_status = status

        def timer_callback():
            result = run.poll_process()
            if result is None:
                _ACTIVE_TIMERS.discard(timer_callback)
            return result

        run.timer = timer_callback
        _ACTIVE_TIMERS.add(timer_callback)
        bpy.app.timers.register(
            timer_callback,
            first_interval=0.2,
            persistent=True,
        )
        return {"FINISHED"}


def shutdown() -> None:
    """애드온 해제 시 자신이 시작한 작업자 프로세스를 남기지 않는다."""

    native_input.shutdown()

    for timer in tuple(_ACTIVE_TIMERS):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
        _ACTIVE_TIMERS.discard(timer)
    for run in tuple(_ACTIVE_RUNS):
        if run.timer is not None and bpy.app.timers.is_registered(run.timer):
            bpy.app.timers.unregister(run.timer)
        run.timer = None
        run.cleanup_job_files()
        _ACTIVE_RUNS.remove(run)
    for process in tuple(_ACTIVE_PROCESSES):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        _ACTIVE_PROCESSES.discard(process)
    for job_dir in tuple(_ACTIVE_JOB_DIRS):
        for name in ("request.json", "response.json"):
            (job_dir / name).unlink(missing_ok=True)
        try:
            job_dir.rmdir()
        except OSError:
            pass
        _ACTIVE_JOB_DIRS.discard(job_dir)


class UVMAPPING_OT_add_reference_images(Operator, ImportHelper):
    """파일 선택기에서 여러 참조 이미지를 추가한다."""

    bl_idname = "uvmapping.add_reference_images"
    bl_label = "참조 이미지 추가"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".png"
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp", options={"HIDDEN"})
    files: CollectionProperty(type=OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    # ImportHelper는 filepath만 제공하므로, 다중 선택에 필요한 directory는 직접 선언한다.
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, _context):
        return not _ACTIVE_RUNS

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        directory = Path(self.directory) if self.directory else Path(self.filepath).parent
        names = [item.name for item in self.files if item.name]
        candidates = [directory / name for name in names] or [Path(self.filepath)]
        existing = {_absolute_path(item.path) for item in settings.reference_images}
        added = 0
        for path in candidates:
            resolved = path.expanduser().resolve()
            if resolved.suffix.lower() not in SUPPORTED_EXTENSIONS or resolved in existing:
                continue
            if len(settings.reference_images) >= MAX_REFERENCE_IMAGES:
                break
            item = settings.reference_images.add()
            item.path = str(resolved)
            item.label = resolved.name
            existing.add(resolved)
            added += 1
        settings.reference_image_index = max(0, len(settings.reference_images) - 1)
        if added:
            settings.texture_analysis_json = ""
            settings.texture_analysis_reference_hash = ""
        settings.texture_status = f"참조 이미지 {added}장 추가됨"
        return {"FINISHED"}


class UVMAPPING_OT_paste_reference_image(Operator):
    """브라우저에서 복사한 이미지를 참조 목록에 PNG로 추가한다."""

    bl_idname = "uvmapping.paste_reference_image"
    bl_label = "클립보드에서 붙여넣기"
    bl_description = "Pinterest나 브라우저에서 복사한 이미지를 PNG 참조로 추가합니다"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = getattr(getattr(context, "scene", None), "uvmapping_settings", None)
        return (
            clipboard_image.is_supported()
            and not _ACTIVE_RUNS
            and settings is not None
            and len(settings.reference_images) < MAX_REFERENCE_IMAGES
        )

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        if len(settings.reference_images) >= MAX_REFERENCE_IMAGES:
            self.report({"ERROR"}, f"참조 이미지는 최대 {MAX_REFERENCE_IMAGES}장까지 사용할 수 있습니다.")
            return {"CANCELLED"}
        path, error = clipboard_image.paste_to(str(_reference_output_directory()))
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        try:
            validated_path, _mime_type = validate_reference_image_path(path)
        except ValueError as exc:
            Path(path).unlink(missing_ok=True)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        resolved = validated_path.resolve()
        item = settings.reference_images.add()
        item.path = str(resolved)
        item.label = resolved.name
        settings.reference_image_index = len(settings.reference_images) - 1
        settings.texture_analysis_json = ""
        settings.texture_analysis_reference_hash = ""
        settings.texture_status = f"클립보드 참조 추가됨: {resolved.name}"
        self.report({"INFO"}, settings.texture_status)
        return {"FINISHED"}


class UVMAPPING_OT_edit_texture_prompt(Operator):
    """OS 네이티브 다이얼로그에서 한글 프롬프트를 안정적으로 입력한다."""

    bl_idname = "uvmapping.edit_texture_prompt"
    bl_label = "한글 프롬프트 입력"
    bl_description = "Blender 한글 조합 문제를 피하도록 OS 입력 창에서 추가 지시를 작성합니다"

    @classmethod
    def poll(cls, _context):
        return native_input.is_supported() and not native_input.is_open()

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        scene_pointer = context.scene.as_pointer()

        def on_done(text):
            if text is None:
                return
            scene = next(
                (
                    candidate
                    for candidate in bpy.data.scenes
                    if candidate.as_pointer() == scene_pointer
                ),
                None,
            )
            if scene is not None and hasattr(scene, "uvmapping_settings"):
                scene.uvmapping_settings.texture_user_prompt = native_input.to_single_line(text)

        error = native_input.open_dialog(
            "AI 텍스처 추가 지시",
            settings.texture_user_prompt,
            on_done,
        )
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        return {"FINISHED"}


class UVMAPPING_OT_remove_reference_image(Operator):
    """선택된 참조 이미지를 목록에서 제거한다."""

    bl_idname = "uvmapping.remove_reference_image"
    bl_label = "참조 이미지 제거"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(getattr(context, "scene", None), "uvmapping_settings", None)
        return not _ACTIVE_RUNS and settings is not None and bool(settings.reference_images)

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        index = settings.reference_image_index
        if 0 <= index < len(settings.reference_images):
            settings.reference_images.remove(index)
            settings.reference_image_index = min(index, max(0, len(settings.reference_images) - 1))
            settings.texture_analysis_json = ""
            settings.texture_analysis_reference_hash = ""
            settings.texture_status = "참조가 변경되어 다시 분석해야 합니다"
        return {"FINISHED"}


class UVMAPPING_OT_analyze_references(_AsyncTextureMixin, Operator):
    """참조 이미지에서 손맵 스타일과 디자인 규칙을 구조화해 추출한다."""

    bl_idname = "uvmapping.analyze_references"
    bl_label = "참조 이미지 분석"

    @classmethod
    def poll(cls, _context):
        return not _ACTIVE_RUNS

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        try:
            paths = _reference_paths(settings)
            if not bpy.app.online_access:
                raise ValueError(
                    "Blender 환경설정 > 시스템에서 'Allow Online Access'를 켜 주세요."
                )
            model, _image_model = _resolved_models(settings)
            api_key = resolve_api_key(context)
            payload = {"reference_digest": _reference_digest(paths)}
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        job = {
            "action": "analyze",
            "model": model,
            "prompt": build_reference_analysis_prompt(len(paths)),
            "image_paths": [str(path) for path in paths],
        }
        return self._start(
            context, job, api_key, "참조 이미지 분석 중…", payload, _finish_analysis
        )


class UVMAPPING_OT_generate_turnaround(_AsyncTextureMixin, Operator):
    """실제 모델 형상과 참조 스타일로 한 장짜리 3면도를 생성한다."""

    bl_idname = "uvmapping.generate_turnaround"
    bl_label = "단일 3면도 생성"

    @classmethod
    def poll(cls, context):
        return not _ACTIVE_RUNS and bool(texture_targets(context))

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        try:
            if not bpy.app.online_access:
                raise ValueError(
                    "Blender 환경설정 > 시스템에서 'Allow Online Access'를 켜 주세요."
                )
            objects = _validated_texture_targets(context)
            reference_paths = _reference_paths(settings, allow_empty=True)
            if reference_paths:
                if settings.texture_analysis_reference_hash != _reference_digest(
                    reference_paths
                ):
                    settings.texture_analysis_json = ""
                    settings.texture_analysis_reference_hash = ""
                    raise ValueError("참조 이미지가 변경되었습니다. 다시 분석해 주세요.")
                analysis = parse_reference_analysis(settings.texture_analysis_json)
            elif settings.texture_user_prompt.strip():
                # 참조 이미지가 없으면 사용자 프롬프트만으로 스타일을 정한다.
                analysis = None
            else:
                raise ValueError(
                    "참조 이미지가 없을 때는 추가 지시 프롬프트를 입력해 주세요."
                )
            # 이미지 모델은 참조 원본을 주면 그 캐릭터를 그대로 재생성해 모델
            # 실루엣을 무시한다. 기본은 분석 결과(텍스트)만 스타일 근거로 보낸다.
            generation_references = (
                reference_paths if settings.send_reference_images else ()
            )
            _analysis_model, model = _resolved_models(settings)
            api_key = resolve_api_key(context)
            projection = _projection_contract(context, objects)
            model_paths = _render_model_views(context, objects, projection)
            output_path = _output_path(context, objects)
            contact_sheet = _join_horizontal(
                model_paths,
                output_path.with_name(f"{output_path.stem}_geometry.png"),
            )
            for path in model_paths:
                path.unlink(missing_ok=True)
            try:
                model_paths[0].parent.rmdir()
            except OSError:
                pass
            user_prompt = settings.texture_user_prompt
            target_keys = tuple((obj.name, obj.session_uid) for obj in objects)
            reference_state = tuple(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in reference_paths
            )
            self_reference_count = len(generation_references)
            source_jobs = tuple(
                {
                    "object_name": obj.name,
                    **{
                        key: job.get(key, "")
                        for key in (
                            "job_id",
                            "atlas_id",
                            "atlas_hash",
                            "atlas_member_id",
                            "uv_hash",
                            "mesh_hash",
                            "uv_layer_name",
                            "target_resolution",
                            "requested_padding",
                        )
                    },
                }
                for obj, job in (
                    (obj, json.loads(obj[TEXTURE_JOB_PROPERTY])) for obj in objects
                )
            )
            payload = {
                "target_keys": target_keys,
                "target_names": tuple(name for name, _session_uid in target_keys),
                "contact_sheet_path": str(contact_sheet),
                "reference_state": reference_state,
                "source_jobs": source_jobs,
                "projection": projection,
                "user_prompt": user_prompt,
                "model": model,
                "analysis_payload": analysis.to_dict() if analysis is not None else None,
            }
        except (ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        job = {
            "action": "turnaround",
            "model": model,
            "prompt": compile_turnaround_prompt(
                analysis, user_prompt, reference_image_count=self_reference_count
            ),
            "image_paths": [
                str(contact_sheet),
                *(str(path) for path in generation_references),
            ],
            "output_path": str(output_path),
        }
        return self._start(
            context,
            job,
            api_key,
            "한 번의 요청으로 3면도 생성 중…",
            payload,
            _finish_turnaround,
        )


def _finish_analysis(scene, value: dict, payload: dict) -> None:
    settings = scene.uvmapping_settings
    analysis = parse_reference_analysis(str(value["text"]))
    settings.texture_analysis_json = json.dumps(
        analysis.to_dict(), ensure_ascii=False, indent=2
    )
    settings.texture_analysis_reference_hash = payload["reference_digest"]
    settings.texture_status = "참조 분석 완료"


def _finish_turnaround(scene, value: dict, payload: dict) -> None:
    value = Path(str(value["output_path"]))
    settings = scene.uvmapping_settings
    settings.texture_output_path = str(value)
    settings.texture_status = "3면도 생성 완료 · Diffuse/Albedo를 적용해 주세요"
    crop_paths = _crop_turnaround(value)
    state = {
        "schema_version": "1.1",
        "status": "TURNAROUND_READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": "openrouter",
        "model": payload["model"],
        "target_objects": payload["target_names"],
        "source_jobs": payload["source_jobs"],
        "projection": payload["projection"],
        "references": payload["reference_state"],
        "user_prompt": payload["user_prompt"],
        "analysis": payload["analysis_payload"],
        "geometry_contact_sheet": payload["contact_sheet_path"],
        "geometry_sha256": hashlib.sha256(
            Path(payload["contact_sheet_path"]).read_bytes()
        ).hexdigest(),
        "turnaround_path": str(value),
        "turnaround_sha256": hashlib.sha256(value.read_bytes()).hexdigest(),
        "views": {
            "front": str(crop_paths[0]),
            "right": str(crop_paths[1]),
            "back": str(crop_paths[2]),
        },
        "view_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in zip(("front", "right", "back"), crop_paths)
        },
    }
    encoded_state = json.dumps(state, ensure_ascii=False, sort_keys=True)
    targets = []
    for name, session_uid in payload["target_keys"]:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.session_uid == session_uid:
            obj[TEXTURE_DESIGN_STATE_PROPERTY] = encoded_state
            targets.append(obj)
    try:
        bpy.data.images.load(str(value), check_existing=False)
    except RuntimeError:
        pass
    if not settings.auto_apply_diffuse:
        return
    if len(targets) != len(payload["target_keys"]):
        settings.texture_status = (
            "3면도는 생성됐지만 대상 객체가 바뀌어 자동 적용을 건너뜁니다. "
            "대상을 다시 지정하고 Diffuse/Albedo를 적용해 주세요."
        )
        return
    try:
        # 생성 시점과 같은 객체에 바로 이어서 굽는다. 로컬 처리라 추가 비용이 없다.
        with _bake_context(scene) as context:
            apply_diffuse(context, tuple(targets))
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        settings.texture_status = f"3면도는 생성됐지만 자동 적용 실패: {exc}"


@contextmanager
def _bake_context(scene):
    """타이머에서 베이크할 때 3면도를 만든 Scene을 가리키는 context를 만든다."""

    context = bpy.context
    if getattr(context, "scene", None) is scene:
        yield context
        return
    window = next(
        (
            candidate
            for candidate in getattr(context.window_manager, "windows", ())
            if candidate.scene is scene
        ),
        None,
    )
    if window is None:
        raise RuntimeError("3면도를 만든 Scene을 화면에서 찾지 못했습니다.")
    with context.temp_override(window=window, scene=scene):
        yield bpy.context


def _diffuse_output_path(state: dict, objects: tuple) -> Path:
    """첫 적용은 고유 경로를 만들고 무과금 재적용은 같은 파일을 갱신한다."""

    existing = state.get("albedo_path")
    if existing:
        return Path(str(existing))
    turnaround = Path(str(state["turnaround_path"]))
    safe_name = bpy.path.clean_name(objects[0].name) if len(objects) == 1 else "selection"
    candidate = turnaround.with_name(f"{safe_name}_albedo.png")
    index = 1
    while candidate.exists():
        candidate = turnaround.with_name(f"{safe_name}_albedo_{index:02d}.png")
        index += 1
    return candidate


def apply_diffuse(context, objects=None) -> tuple[str, str]:
    """검증부터 머티리얼 적용까지 수행하고 (상태 문자열, 경고) 쌍을 돌려준다.

    실패는 예외로 올려 호출자가 연산자 보고나 상태 문자열로 처리하게 한다.
    """

    settings = context.scene.uvmapping_settings
    objects, state, jobs = _validated_bake_targets(context, objects)
    resolutions = {
        tuple(int(value) for value in job.get("target_resolution", ()))
        for job in jobs
        if job.get("target_resolution")
    }
    if len(resolutions) > 1:
        raise ValueError("대상 객체들의 TextureJob 해상도가 서로 다릅니다.")
    target_resolution = next(iter(resolutions), ())
    if target_resolution and (
        len(target_resolution) != 2 or target_resolution[0] != target_resolution[1]
    ):
        raise ValueError("현재 버전은 정사각형 0-1 Atlas만 지원합니다.")
    resolution = target_resolution[0] if target_resolution else int(
        settings.texture_resolution
    )
    paddings = {
        int(job["requested_padding"])
        for job in jobs
        if job.get("requested_padding") is not None
    }
    if len(paddings) > 1:
        raise ValueError("대상 객체들의 TextureJob 패딩이 서로 다릅니다.")
    padding = next(iter(paddings), int(settings.padding_pixels))
    uv_layer_names = tuple(
        str(job.get("uv_layer_name") or obj.data.uv_layers.active.name)
        for obj, job in zip(objects, jobs)
    )
    output_path = _diffuse_output_path(state, objects)
    view_paths = tuple(Path(state["views"][name]) for name in ("front", "right", "back"))
    settings.texture_status = "3면도에서 Diffuse/Albedo 베이크 중…"
    result = texture_bake.bake_diffuse(
        context,
        objects,
        view_paths,
        output_path,
        resolution,
        padding,
        uv_layer_names=uv_layer_names,
    )

    output_path = Path(str(result.get("output_path", output_path)))
    bake_stats = {
        key: result[key]
        for key in (
            "filled_pixels",
            "dilated_pixels",
            "occluded_samples",
            "occluded_fallback_pixels",
            "fallback_pixels",
            "depth_resolution",
            "outside_atlas_triangles",
            "triangle_count",
        )
        if key in result
    }
    updated = dict(state)
    updated.update(
        {
            "status": "ALBEDO_APPLIED",
            "albedo_applied_at": datetime.now(timezone.utc).isoformat(),
            "albedo_path": str(output_path),
            "albedo_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
            "albedo_resolution": [resolution, resolution],
            "albedo_padding": padding,
            "material_name": str(result.get("material_name", "")),
            "bake_stats": bake_stats,
        }
    )
    encoded_state = json.dumps(updated, ensure_ascii=False, sort_keys=True)
    for obj in objects:
        obj[TEXTURE_DESIGN_STATE_PROPERTY] = encoded_state
    settings.texture_diffuse_path = str(output_path)

    warning = ""
    outside = int(bake_stats.get("outside_atlas_triangles", 0))
    if outside:
        # Mirror·Array의 UV Offset처럼 평가 UV를 0-1 밖으로 미는 설정은
        # 해당 면을 비워 둔 채 베이크된다.
        warning = (
            f"{outside}개 삼각형의 UV가 0-1 밖이라 그 부분이 비었습니다. "
            "Modifier의 UV Offset을 끄거나 UV를 0-1 안으로 옮긴 뒤 다시 실행해 주세요."
        )
    status = warning or "Diffuse/Albedo 베이크 및 머티리얼 적용 완료"
    settings.texture_status = status
    return status, warning


class UVMAPPING_OT_bake_diffuse(Operator):
    """생성된 3면도를 현재 UV Atlas에 투영하고 머티리얼로 적용한다."""

    bl_idname = "uvmapping.bake_diffuse"
    bl_label = "Diffuse/Albedo 적용"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return can_bake_diffuse(context)

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        try:
            status, warning = apply_diffuse(context)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            message = f"Diffuse/Albedo 적용 실패: {exc}"
            settings.texture_status = message
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        self.report({"WARNING"} if warning else {"INFO"}, status)
        return {"FINISHED"}


class UVMAPPING_OT_add_target_objects(Operator):
    """현재 선택한 Mesh 객체를 텍스처 대상 목록에 등록한다."""

    bl_idname = "uvmapping.add_target_objects"
    bl_label = "선택 객체 등록"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context))

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        existing = {item.object.name for item in settings.target_objects if item.object}
        added = 0
        for obj in _selected_meshes(context):
            if obj.name in existing:
                continue
            entry = settings.target_objects.add()
            entry.object = obj
            existing.add(obj.name)
            added += 1
        settings.target_object_index = max(0, len(settings.target_objects) - 1)
        if not added:
            self.report({"INFO"}, "이미 등록된 객체입니다.")
        else:
            self.report({"INFO"}, f"대상 객체 {added}개를 등록했습니다.")
        return {"FINISHED"}


class UVMAPPING_OT_remove_target_object(Operator):
    """대상 목록에서 선택한 항목을 제거한다."""

    bl_idname = "uvmapping.remove_target_object"
    bl_label = "대상 제거"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.uvmapping_settings.target_objects)

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        index = settings.target_object_index
        if 0 <= index < len(settings.target_objects):
            settings.target_objects.remove(index)
            settings.target_object_index = min(
                index, max(0, len(settings.target_objects) - 1)
            )
        return {"FINISHED"}


class UVMAPPING_OT_clear_target_objects(Operator):
    """대상 목록을 비워 다시 현재 선택을 따르게 한다."""

    bl_idname = "uvmapping.clear_target_objects"
    bl_label = "대상 비우기"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.uvmapping_settings.target_objects)

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        settings.target_objects.clear()
        settings.target_object_index = 0
        self.report({"INFO"}, "대상 목록을 비웠습니다. 현재 선택을 사용합니다.")
        return {"FINISHED"}


classes = (
    UVMAPPING_OT_add_target_objects,
    UVMAPPING_OT_remove_target_object,
    UVMAPPING_OT_clear_target_objects,
    UVMAPPING_OT_add_reference_images,
    UVMAPPING_OT_paste_reference_image,
    UVMAPPING_OT_remove_reference_image,
    UVMAPPING_OT_edit_texture_prompt,
    UVMAPPING_OT_analyze_references,
    UVMAPPING_OT_generate_turnaround,
    UVMAPPING_OT_bake_diffuse,
)


__all__ = ("classes", "shutdown")
