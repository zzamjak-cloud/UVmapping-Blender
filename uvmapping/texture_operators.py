"""참조 분석과 단일 3면도 생성을 Blender에 연결하는 연산자."""

from __future__ import annotations

from array import array
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
from .texture_pipeline import (
    build_reference_analysis_prompt,
    compile_turnaround_prompt,
    parse_reference_analysis,
    validate_reference_image_path,
)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_REFERENCE_IMAGES = 5
TEXTURE_JOB_PROPERTY = "uvmapping_texture_job"
TEXTURE_DESIGN_STATE_PROPERTY = "uvmapping_texture_design_state"
_ACTIVE_PROCESSES: set[subprocess.Popen] = set()
_ACTIVE_JOB_DIRS: set[Path] = set()
_ACTIVE_OPERATORS: list = []
# 연산자 RNA가 해제된 뒤에도 타이머를 안전하게 해제하기 위한 모듈 레지스트리.
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


def _provider_api_key(context, provider: str) -> str:
    preferences = get_addon_preferences(context)
    if provider == "OPENAI":
        api_key = getattr(preferences, "openai_api_key", "").strip() or os.environ.get(
            "OPENAI_API_KEY", ""
        ).strip()
        provider_name = "OpenAI"
    else:
        api_key = getattr(preferences, "gemini_api_key", "").strip() or os.environ.get(
            "GEMINI_API_KEY", ""
        ).strip()
        provider_name = "Gemini"
    if not api_key:
        raise ValueError(
            f"Blender 애드온 환경설정에 {provider_name} API 키를 입력해 주세요."
        )
    return api_key


def _provider_models(settings) -> tuple[str, str, str]:
    """UI에서 선택한 모델에 대응하는 Provider·분석·이미지 모델을 반환한다."""

    provider = str(settings.texture_image_provider)
    if provider == "OPENAI":
        return (
            provider,
            settings.texture_openai_analysis_model.strip(),
            settings.texture_openai_image_model.strip(),
        )
    return (
        "GEMINI",
        settings.texture_analysis_model.strip(),
        settings.texture_image_model.strip(),
    )


def _selected_meshes(context) -> tuple:
    candidates = getattr(context, "selected_editable_objects", ())
    return tuple(
        obj
        for obj in candidates
        if obj.type == "MESH" and obj.data is not None and len(obj.data.polygons) > 0
    )


def _validated_texture_targets(context) -> tuple:
    objects = _selected_meshes(context)
    if not objects:
        raise ValueError("텍스처를 만들 Mesh 객체를 선택해 주세요.")
    jobs = []
    for obj in objects:
        raw_job = obj.get(TEXTURE_JOB_PROPERTY)
        if not raw_job:
            raise ValueError(f"{obj.name}: 먼저 Auto UV Unwrap을 실행해 주세요.")
        try:
            jobs.append(json.loads(raw_job))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{obj.name}: TextureJob 정보가 올바르지 않습니다.") from exc
    atlas_ids = {str(job.get("atlas_id", "")) for job in jobs}
    if len(objects) > 1 and (len(atlas_ids) != 1 or "" in atlas_ids):
        raise ValueError("선택 객체들이 같은 TextureJob Atlas에 속하지 않습니다.")
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


def _validated_bake_targets(context) -> tuple[tuple, dict, tuple[dict, ...]]:
    """현재 선택·UV·Transform이 3면도 생성 시점과 같은지 검사한다."""

    objects = _validated_texture_targets(context)
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
        raise ValueError("3면도를 생성했던 Mesh 객체를 모두 다시 선택해 주세요.")
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

    objects = _selected_meshes(context)
    return bool(objects) and not _ACTIVE_OPERATORS and all(
        obj.get(TEXTURE_DESIGN_STATE_PROPERTY) for obj in objects
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
    saved = {
        "camera": scene.camera,
        "engine": render.engine,
        "filepath": render.filepath,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "film_transparent": render.film_transparent,
        "file_format": render.image_settings.file_format,
        "shading_light": scene.display.shading.light,
        "shading_color_type": scene.display.shading.color_type,
        "shading_single_color": tuple(scene.display.shading.single_color),
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
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "SINGLE"
        scene.display.shading.single_color = (0.55, 0.55, 0.55)
        scene.camera = camera
        render.resolution_x = 512
        render.resolution_y = 512
        render.resolution_percentage = 100
        render.film_transparent = True
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
        scene.display.shading.light = saved["shading_light"]
        scene.display.shading.color_type = saved["shading_color_type"]
        scene.display.shading.single_color = saved["shading_single_color"]
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)
    return tuple(paths)


def _join_horizontal(paths: tuple[Path, ...], output_path: Path) -> Path:
    """동일 크기 모델 뷰를 라벨 없이 한 장의 가로 contact sheet로 합친다."""

    sources = [bpy.data.images.load(str(path), check_existing=False) for path in paths]
    target = None
    try:
        width, height = sources[0].size
        if any(tuple(image.size) != (width, height) for image in sources):
            raise RuntimeError("모델 뷰 렌더 크기가 서로 다릅니다.")
        channels = 4
        target_width = width * len(sources)
        pixels = array("f", [0.0]) * (target_width * height * channels)
        for column, source in enumerate(sources):
            source_pixels = array("f", [0.0]) * (width * height * channels)
            source.pixels.foreach_get(source_pixels)
            for row in range(height):
                source_start = row * width * channels
                target_start = (row * target_width + column * width) * channels
                pixels[target_start : target_start + width * channels] = source_pixels[
                    source_start : source_start + width * channels
                ]
        target = bpy.data.images.new(
            "UVMapping Model Contact Sheet",
            width=target_width,
            height=height,
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


class _AsyncTextureMixin:
    """별도 Blender 프로세스를 앱 타이머로 감시하는 연산자 공통부."""

    _app_timer = None
    _process = None
    _request_path = None
    _response_path = None
    _cancel_notice_shown = False

    def _start(self, context, job: dict, api_key: str, status: str):
        if _ACTIVE_OPERATORS:
            self.report({"WARNING"}, "이미 AI 작업이 진행 중입니다.")
            return {"CANCELLED"}
        job_dir = Path(tempfile.gettempdir()) / "uvmapping_ai" / uuid.uuid4().hex
        job_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        _ACTIVE_JOB_DIRS.add(job_dir)
        self._request_path = job_dir / "request.json"
        self._response_path = job_dir / "response.json"
        descriptor = os.open(
            self._request_path,
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
        try:
            self._process = subprocess.Popen(
                (
                    bpy.app.binary_path,
                    "--background",
                    "--factory-startup",
                    "--python",
                    str(worker_path),
                    "--",
                    str(self._request_path),
                    str(self._response_path),
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            if self._process.stdin is None:
                raise OSError("AI 작업자 표준입력을 열지 못했습니다.")
            self._process.stdin.write((api_key + "\n").encode("utf-8"))
            self._process.stdin.close()
        except OSError as exc:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            self._cleanup_job_files()
            message = f"AI 작업자 프로세스를 시작하지 못했습니다: {exc}"
            context.scene.uvmapping_settings.texture_status = message
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        _ACTIVE_PROCESSES.add(self._process)
        _ACTIVE_OPERATORS.append(self)
        self._scene_pointer = context.scene.as_pointer()
        context.scene.uvmapping_settings.texture_status = status

        # Blender가 연산자 RNA를 먼저 해제해도(종료·리로드) 타이머가
        # ReferenceError 없이 스스로 정리되도록 closure로 감싼다.
        def timer_callback():
            try:
                result = self._poll_process()
            except ReferenceError:
                result = None
            if result is None:
                _ACTIVE_TIMERS.discard(timer_callback)
            return result

        self._app_timer = timer_callback
        _ACTIVE_TIMERS.add(timer_callback)
        bpy.app.timers.register(
            timer_callback,
            first_interval=0.2,
            persistent=True,
        )
        return {"FINISHED"}

    def _poll_process(self):
        if self._process.poll() is None:
            return 0.2
        _ACTIVE_PROCESSES.discard(self._process)
        if self in _ACTIVE_OPERATORS:
            _ACTIVE_OPERATORS.remove(self)
        scene = next(
            (
                candidate
                for candidate in bpy.data.scenes
                if candidate.as_pointer() == self._scene_pointer
            ),
            None,
        )
        try:
            result = json.loads(self._response_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": f"AI 작업 결과를 읽지 못했습니다: {exc}"}
        self._cleanup_job_files()
        if not result.get("ok"):
            message = str(result.get("error", "알 수 없는 AI 작업 오류"))
            if scene is not None:
                scene.uvmapping_settings.texture_status = message
            return None
        if scene is None:
            self._preserve_result_without_scene(result)
            return None
        try:
            self._finish(scene, result)
        except Exception as exc:
            if result.get("output_path"):
                scene.uvmapping_settings.texture_output_path = str(result["output_path"])
                message = f"원본 AI 결과는 저장됐지만 후처리에 실패했습니다: {exc}"
            else:
                message = f"AI 결과 적용 실패: {exc}"
            scene.uvmapping_settings.texture_status = message
        return None

    def _preserve_result_without_scene(self, result: dict) -> None:
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
        for name, session_uid in getattr(self, "_target_keys", ()):
            obj = bpy.data.objects.get(name)
            if obj is not None and obj.session_uid == session_uid:
                obj[TEXTURE_DESIGN_STATE_PROPERTY] = state

    def _cleanup_job_files(self):
        for path in (self._request_path, self._response_path):
            if path is not None:
                path.unlink(missing_ok=True)
        if self._request_path is not None:
            _ACTIVE_JOB_DIRS.discard(self._request_path.parent)
            try:
                self._request_path.parent.rmdir()
            except OSError:
                pass


def shutdown() -> None:
    """애드온 해제 시 자신이 시작한 작업자 프로세스를 남기지 않는다."""

    native_input.shutdown()

    for timer in tuple(_ACTIVE_TIMERS):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
        _ACTIVE_TIMERS.discard(timer)
    for operator in tuple(_ACTIVE_OPERATORS):
        try:
            timer = operator._app_timer
            if timer is not None and bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)
            operator._app_timer = None
            operator._cleanup_job_files()
        except ReferenceError:
            # 연산자 RNA가 이미 해제됨 — 타이머·프로세스·작업 파일은
            # 모듈 레지스트리(_ACTIVE_TIMERS 등)가 정리한다.
            pass
        _ACTIVE_OPERATORS.remove(operator)
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

    @classmethod
    def poll(cls, _context):
        return not _ACTIVE_OPERATORS

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        directory = Path(self.directory)
        candidates = [directory / item.name for item in self.files] or [Path(self.filepath)]
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
            and not _ACTIVE_OPERATORS
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
        return not _ACTIVE_OPERATORS and settings is not None and bool(settings.reference_images)

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
        return not _ACTIVE_OPERATORS

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        try:
            paths = _reference_paths(settings)
            if not bpy.app.online_access:
                raise ValueError(
                    "Blender 환경설정 > 시스템에서 'Allow Online Access'를 켜 주세요."
                )
            provider, model, _image_model = _provider_models(settings)
            api_key = _provider_api_key(context, provider)
            self._analysis_reference_digest = _reference_digest(paths)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        job = {
            "action": "analyze",
            "provider": provider,
            "model": model,
            "prompt": build_reference_analysis_prompt(len(paths)),
            "image_paths": [str(path) for path in paths],
        }
        return self._start(context, job, api_key, "참조 이미지 분석 중…")

    def _finish(self, scene, value):
        settings = scene.uvmapping_settings
        analysis = parse_reference_analysis(str(value["text"]))
        settings.texture_analysis_json = json.dumps(
            analysis.to_dict(), ensure_ascii=False, indent=2
        )
        settings.texture_analysis_reference_hash = self._analysis_reference_digest
        settings.texture_status = "참조 분석 완료"


class UVMAPPING_OT_generate_turnaround(_AsyncTextureMixin, Operator):
    """실제 모델 형상과 참조 스타일로 한 장짜리 3면도를 생성한다."""

    bl_idname = "uvmapping.generate_turnaround"
    bl_label = "단일 3면도 생성"

    @classmethod
    def poll(cls, context):
        return not _ACTIVE_OPERATORS and bool(_selected_meshes(context))

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
            provider, _analysis_model, model = _provider_models(settings)
            api_key = _provider_api_key(context, provider)
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
            self._target_keys = tuple((obj.name, obj.session_uid) for obj in objects)
            self._target_names = tuple(name for name, _session_uid in self._target_keys)
            self._contact_sheet_path = str(contact_sheet)
            self._reference_state = tuple(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in reference_paths
            )
            self._source_jobs = tuple(
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
            self._projection = projection
            self._user_prompt = user_prompt
            self._model = model
            self._provider = provider
            self._analysis_payload = (
                analysis.to_dict() if analysis is not None else None
            )
        except (ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        job = {
            "action": "turnaround",
            "provider": provider,
            "model": model,
            "prompt": compile_turnaround_prompt(analysis, user_prompt),
            "image_paths": [str(contact_sheet), *(str(path) for path in reference_paths)],
            "output_path": str(output_path),
        }
        return self._start(context, job, api_key, "한 번의 요청으로 3면도 생성 중…")

    def _finish(self, scene, value):
        value = Path(str(value["output_path"]))
        settings = scene.uvmapping_settings
        settings.texture_output_path = str(value)
        settings.texture_status = "3면도 생성 완료 · Diffuse/Albedo를 적용해 주세요"
        crop_paths = _crop_turnaround(value)
        state = {
            "schema_version": "1.1",
            "status": "TURNAROUND_READY",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider": (
                "openai" if self._provider == "OPENAI" else "google_gemini"
            ),
            "model": self._model,
            "target_objects": self._target_names,
            "source_jobs": self._source_jobs,
            "projection": self._projection,
            "references": self._reference_state,
            "user_prompt": self._user_prompt,
            "analysis": self._analysis_payload,
            "geometry_contact_sheet": self._contact_sheet_path,
            "geometry_sha256": hashlib.sha256(
                Path(self._contact_sheet_path).read_bytes()
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
        for name, session_uid in self._target_keys:
            obj = bpy.data.objects.get(name)
            if obj is not None and obj.session_uid == session_uid:
                obj[TEXTURE_DESIGN_STATE_PROPERTY] = encoded_state
        try:
            bpy.data.images.load(str(value), check_existing=False)
        except RuntimeError:
            pass


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


class UVMAPPING_OT_bake_diffuse(Operator):
    """생성된 3면도를 현재 Auto UV Atlas에 투영하고 머티리얼로 적용한다."""

    bl_idname = "uvmapping.bake_diffuse"
    bl_label = "Diffuse/Albedo 적용"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return can_bake_diffuse(context)

    def execute(self, context):
        settings = context.scene.uvmapping_settings
        try:
            objects, state, jobs = _validated_bake_targets(context)
            resolutions = {
                tuple(int(value) for value in job.get("target_resolution", ()))
                for job in jobs
                if job.get("target_resolution")
            }
            if len(resolutions) > 1:
                raise ValueError("선택 객체들의 TextureJob 해상도가 서로 다릅니다.")
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
                raise ValueError("선택 객체들의 TextureJob 패딩이 서로 다릅니다.")
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
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            message = f"Diffuse/Albedo 적용 실패: {exc}"
            settings.texture_status = message
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        output_path = Path(str(result.get("output_path", output_path)))
        updated = dict(state)
        bake_stats = {
            key: result[key]
            for key in (
                "filled_pixels",
                "dilated_pixels",
                "occluded_samples",
                "fallback_pixels",
                "depth_resolution",
            )
            if key in result
        }
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
        settings.texture_status = "Diffuse/Albedo 베이크 및 머티리얼 적용 완료"
        self.report({"INFO"}, settings.texture_status)
        return {"FINISHED"}


classes = (
    UVMAPPING_OT_add_reference_images,
    UVMAPPING_OT_paste_reference_image,
    UVMAPPING_OT_remove_reference_image,
    UVMAPPING_OT_edit_texture_prompt,
    UVMAPPING_OT_analyze_references,
    UVMAPPING_OT_generate_turnaround,
    UVMAPPING_OT_bake_diffuse,
)


__all__ = ("classes", "shutdown")
