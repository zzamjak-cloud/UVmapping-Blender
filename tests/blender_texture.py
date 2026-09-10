"""격리 Blender에서 AI 텍스처 UI와 로컬 이미지 단계를 검사한다."""

from __future__ import annotations

import importlib
import hashlib
import io
import inspect
import json
import os
from pathlib import Path
import subprocess
import tempfile

import bpy


ADDON_ID = "uvmapping_blender"
MODULE_NAME = "bl_ext.user_default.uvmapping_blender"


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _assert_isolated_profile() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    profile_root = Path(bpy.utils.resource_path("USER"))
    addon_link = profile_root / "extensions" / "user_default" / ADDON_ID
    assert addon_link.exists(), f"개발 Extension 링크가 없습니다: {addon_link}"
    assert _same_path(addon_link, repository_root)


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _operator_registered() -> bool:
    try:
        bpy.ops.uvmapping.generate_turnaround.get_rna_type()
    except (KeyError, RuntimeError):
        return False
    return True


def _key_configuration_from_new_process() -> tuple[bool, bool]:
    """같은 격리 프로필을 새 Blender 프로세스에서 열어 키 저장 상태만 확인한다."""

    probe_script = f"""
import bpy

addon = bpy.context.preferences.addons.get({MODULE_NAME!r})
assert addon is not None, "개발 Extension이 활성화되지 않았습니다."
preferences = getattr(addon, "preferences", None)
assert preferences is not None, "애드온 환경설정을 찾지 못했습니다."
openrouter_configured = int(bool(getattr(preferences, "openrouter_api_key", "").strip()))
print(f"UVMAPPING_API_KEY_STATE={{openrouter_configured}}")
"""
    result = subprocess.run(
        (
            bpy.app.binary_path,
            "--background",
            "--python-exit-code",
            "1",
            "--python-expr",
            probe_script,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, "새 Blender 프로세스의 환경설정 확인에 실패했습니다."
    marker = "UVMAPPING_API_KEY_STATE="
    state_line = next(
        (line for line in result.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    assert state_line is not None, "새 Blender 프로세스가 API 키 저장 상태를 반환하지 않았습니다."
    value = state_line.removeprefix(marker).strip()
    assert value in ("0", "1"), f"예상치 못한 API 키 상태입니다: {value!r}"
    return value == "1"


def _check_full_pipeline(texture_module, bake_module) -> None:
    """작업자를 스텁으로 바꿔 생성 -> 결과 회수 -> 베이크 -> 저장 전 구간을 검증한다."""

    original_popen = texture_module.subprocess.Popen
    original_key = os.environ.get("OPENROUTER_API_KEY")
    online = bpy.context.preferences.system.use_online_access

    def fake_turnaround(path: Path) -> None:
        width, height = 336, 144  # 21:9 결과의 축소판
        pixels = bytearray()
        for _row in range(height):
            for column in range(width):
                band = min(2, column * 3 // width)
                pixels += bytes(
                    ((200, 60, 60), (60, 200, 60), (60, 60, 200))[band] + (255,)
                )
        path.write_bytes(bake_module.encode_srgb_png(bytes(pixels), width, height))

    class _StubWorker:
        def __init__(self, arguments, **_kwargs):
            self.stdin = io.BytesIO()
            request = json.loads(Path(arguments[-2]).read_text(encoding="utf-8"))
            output_path = Path(request["output_path"])
            fake_turnaround(output_path)
            Path(arguments[-1]).write_text(
                json.dumps({"ok": True, "output_path": str(output_path)}),
                encoding="utf-8",
            )

        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    texture_module.subprocess.Popen = _StubWorker
    os.environ["OPENROUTER_API_KEY"] = "stub-key"
    bpy.context.preferences.system.use_online_access = True
    try:
        _clear_scene()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cube = bpy.context.object
        cube.modifiers.new("Subdivision", "SUBSURF")
        settings = bpy.context.scene.uvmapping_settings
        settings.texture_resolution = "256"
        settings.padding_pixels = 4
        settings.texture_user_prompt = "스텁 파이프라인 검사"
        settings.auto_apply_diffuse = False
        # 대상 등록을 쓰면 선택과 무관하게 그 객체로 진행해야 한다.
        entry = settings.target_objects.add()
        entry.object = cube
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.object.mode_set(mode="OBJECT")

        assert bpy.ops.uvmapping.generate_turnaround() == {"FINISHED"}, settings.texture_status
        # Operator RNA는 execute() 반환과 함께 해제되므로, 결과 회수는
        # 연산자 밖의 실행 레지스트리가 담당해야 한다.
        assert len(texture_module._ACTIVE_RUNS) == 1
        assert texture_module._ACTIVE_RUNS[0].poll_process() is None
        assert not texture_module._ACTIVE_RUNS

        state = json.loads(cube[texture_module.TEXTURE_DESIGN_STATE_PROPERTY])
        assert state["status"] == "TURNAROUND_READY", state["status"]
        assert all(Path(path).is_file() for path in state["views"].values())

        assert texture_module.can_bake_diffuse(bpy.context)
        assert bpy.ops.uvmapping.bake_diffuse() == {"FINISHED"}, settings.texture_status
        diffuse_path = Path(settings.texture_diffuse_path)
        assert diffuse_path.is_file()
        applied = json.loads(cube[texture_module.TEXTURE_DESIGN_STATE_PROPERTY])
        assert applied["status"] == "ALBEDO_APPLIED"
        material = cube.data.materials[cube.data.polygons[0].material_index]
        texture_node = next(
            node
            for node in material.node_tree.nodes
            if node.bl_idname == "ShaderNodeTexImage"
        )
        assert texture_node.image is not None
        for path in (diffuse_path, Path(state["turnaround_path"])):
            path.unlink(missing_ok=True)
        for path in state["views"].values():
            Path(path).unlink(missing_ok=True)
        Path(state["geometry_contact_sheet"]).unlink(missing_ok=True)

        # 자동 적용을 켜면 Edit Mode에서 시작해도 모드 전환과 베이크까지 이어진다.
        settings.auto_apply_diffuse = True
        settings.texture_diffuse_path = ""
        bpy.context.view_layer.objects.active = cube
        cube.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        assert bpy.ops.uvmapping.generate_turnaround() == {"FINISHED"}, settings.texture_status
        assert cube.mode == "OBJECT", "생성 단계에서 Object Mode로 전환하지 못했습니다."
        assert texture_module._ACTIVE_RUNS[0].poll_process() is None
        auto_state = json.loads(cube[texture_module.TEXTURE_DESIGN_STATE_PROPERTY])
        assert auto_state["status"] == "ALBEDO_APPLIED", settings.texture_status
        auto_diffuse = Path(settings.texture_diffuse_path)
        assert auto_diffuse.is_file()
        auto_diffuse.unlink(missing_ok=True)
        Path(auto_state["turnaround_path"]).unlink(missing_ok=True)
        for path in auto_state["views"].values():
            Path(path).unlink(missing_ok=True)
        Path(auto_state["geometry_contact_sheet"]).unlink(missing_ok=True)
        settings.target_objects.clear()
    finally:
        texture_module.subprocess.Popen = original_popen
        bpy.context.preferences.system.use_online_access = online
        if original_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = original_key
    print("[texture] 스텁 작업자 전 구간(생성-회수-베이크-저장) 통과")


def main() -> None:
    _assert_isolated_profile()
    _clear_scene()

    addon = importlib.import_module(MODULE_NAME)
    addon.unregister()
    addon.register()
    assert _operator_registered(), "AI 3면도 연산자가 등록되지 않았습니다."
    bpy.ops.uvmapping.bake_diffuse.get_rna_type()
    bpy.ops.uvmapping.paste_reference_image.get_rna_type()
    bpy.ops.uvmapping.edit_texture_prompt.get_rna_type()

    settings = bpy.context.scene.uvmapping_settings
    properties = settings.bl_rna.properties
    for name in (
        "reference_images",
        "texture_user_prompt",
        "texture_model_preset",
        "texture_analysis_model",
        "texture_image_model",
        "texture_analysis_json",
        "texture_output_path",
        "texture_diffuse_path",
    ):
        assert properties.get(name) is not None, f"AI 텍스처 속성이 없습니다: {name}"
    assert properties.get("texture_api_key") is None, "API 키가 Scene 속성에 남아 있습니다."
    # 구 Provider별 키·모델 속성은 완전히 사라져야 한다.
    for gone in (
        "texture_image_provider",
        "texture_openai_analysis_model",
        "texture_openai_image_model",
    ):
        assert properties.get(gone) is None, f"구 Provider 속성이 남아 있습니다: {gone}"

    preset_items = properties["texture_model_preset"].enum_items
    assert preset_items["NANO_BANANA_PRO"].name == "Nano Banana Pro"
    assert preset_items["GPT_IMAGE"].name == "GPT Image (덕테이프)"
    assert settings.texture_model_preset == "NANO_BANANA_PRO"
    assert settings.texture_analysis_model == "google/gemini-3.7-flash"
    assert settings.texture_image_model == "google/gemini-3-pro-image"
    # 프리셋을 바꾸면 두 모델 식별자가 함께 갱신되어야 한다.
    settings.texture_model_preset = "GPT_IMAGE"
    assert settings.texture_analysis_model == "openai/gpt-5.6-sol"
    assert settings.texture_image_model == "openai/gpt-5.4-image-2"
    settings.texture_model_preset = "NANO_BANANA_PRO"
    assert settings.texture_image_model == "google/gemini-3-pro-image"

    preferences_module = importlib.import_module(f"{MODULE_NAME}.uvmapping.properties")
    assert preferences_module.addon_module_id() == MODULE_NAME
    addon_preferences = preferences_module.get_addon_preferences(bpy.context)
    assert addon_preferences is not None, "등록된 애드온 환경설정을 찾지 못했습니다."
    assert type(addon_preferences).bl_idname == MODULE_NAME
    key_property = addon_preferences.bl_rna.properties.get("openrouter_api_key")
    assert key_property is not None, "OpenRouter API 키 속성이 없습니다."
    assert key_property.subtype == "PASSWORD"
    for gone in ("gemini_api_key", "openai_api_key"):
        assert addon_preferences.bl_rna.properties.get(gone) is None, (
            f"구 Provider 키 속성이 남아 있습니다: {gone}"
        )
    draw_source = inspect.getsource(preferences_module.UVMAPPING_AP_preferences.draw)
    assert 'operator("wm.save_userpref"' in draw_source
    assert "API 키 저장" in draw_source
    assert "입력한 뒤 반드시" in draw_source
    bpy.ops.wm.save_userpref.get_rna_type()

    configured_before_save = bool(addon_preferences.openrouter_api_key.strip())
    assert bpy.ops.wm.save_userpref() == {"FINISHED"}
    assert _key_configuration_from_new_process() == configured_before_save

    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.object
    texture_module = importlib.import_module(f"{MODULE_NAME}.uvmapping.texture_operators")
    assert texture_module._resolved_models(settings) == (
        "google/gemini-3.7-flash",
        "google/gemini-3-pro-image",
    )
    settings.texture_model_preset = "GPT_IMAGE"
    assert texture_module._resolved_models(settings) == (
        "openai/gpt-5.6-sol",
        "openai/gpt-5.4-image-2",
    )
    settings.texture_model_preset = "NANO_BANANA_PRO"
    # 키가 없으면 네트워크 호출 전에 안내와 함께 막혀야 한다.
    if not addon_preferences.openrouter_api_key.strip() and not os.environ.get(
        "OPENROUTER_API_KEY", ""
    ):
        try:
            texture_module.resolve_api_key(bpy.context)
        except ValueError as error:
            assert "OpenRouter API 키" in str(error)
        else:
            raise AssertionError("키가 없을 때 안내 오류가 발생해야 합니다.")
    with tempfile.TemporaryDirectory(prefix="uvmapping-clipboard-operator-") as temp_dir:
        pasted_path = Path(temp_dir) / "clipboard.png"
        pasted_path.write_bytes(
            b"\x89PNG\r\n\x1a\nclipboard\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        original_paste = texture_module.clipboard_image.paste_to
        original_clipboard_support = texture_module.clipboard_image.is_supported
        texture_module.clipboard_image.paste_to = lambda _directory: (
            str(pasted_path),
            None,
        )
        texture_module.clipboard_image.is_supported = lambda: True
        settings.texture_analysis_json = '{"stale":true}'
        settings.texture_analysis_reference_hash = "stale"
        try:
            assert bpy.ops.uvmapping.paste_reference_image() == {"FINISHED"}
        finally:
            texture_module.clipboard_image.paste_to = original_paste
            texture_module.clipboard_image.is_supported = original_clipboard_support
        assert len(settings.reference_images) == 1
        assert _same_path(Path(settings.reference_images[0].path), pasted_path)
        assert settings.texture_analysis_json == ""
        assert settings.texture_analysis_reference_hash == ""
        assert bpy.ops.uvmapping.remove_reference_image() == {"FINISHED"}

    native_module = texture_module.native_input
    original_native_support = native_module.is_supported
    original_native_open = native_module.is_open
    original_open_dialog = native_module.open_dialog
    callback = {}
    native_module.is_supported = lambda: True
    native_module.is_open = lambda: False

    def fake_open_dialog(title, initial_text, on_done):
        callback.update(title=title, initial_text=initial_text, on_done=on_done)
        return None

    native_module.open_dialog = fake_open_dialog
    settings.texture_user_prompt = "기존 지시"
    try:
        assert bpy.ops.uvmapping.edit_texture_prompt() == {"FINISHED"}
        assert callback["initial_text"] == "기존 지시"
        callback["on_done"]("낡은 나무\n  모서리는 밝게")
        assert settings.texture_user_prompt == "낡은 나무 모서리는 밝게"
        callback["on_done"](None)
        assert settings.texture_user_prompt == "낡은 나무 모서리는 밝게"
    finally:
        native_module.is_supported = original_native_support
        native_module.is_open = original_native_open
        native_module.open_dialog = original_open_dialog

    assert texture_module._validated_texture_targets(bpy.context) == (cube,)
    modifier = cube.modifiers.new(name="AI 캡처 경계 테스트", type="ARRAY")
    modifier.count = 3
    modifier.relative_offset_displace = (1.0, 0.0, 0.0)
    minimum, maximum = texture_module._world_bounds(bpy.context, (cube,))
    assert maximum.x - minimum.x > 5.0, "평가된 Modifier 경계를 사용하지 않습니다."
    cube.modifiers.remove(modifier)

    projection_before_shape_change = texture_module._projection_contract(bpy.context, (cube,))
    original_vertex = cube.data.vertices[0].co.copy()
    cube.data.vertices[0].co.x *= 0.5
    projection_after_shape_change = texture_module._projection_contract(bpy.context, (cube,))
    assert projection_before_shape_change["bounds_min"] == projection_after_shape_change["bounds_min"]
    assert projection_before_shape_change["bounds_max"] == projection_after_shape_change["bounds_max"]
    assert (
        projection_before_shape_change["evaluated_geometry_sha256"]
        != projection_after_shape_change["evaluated_geometry_sha256"]
    ), "같은 경계 상자 안의 형상 변경을 감지하지 못했습니다."
    cube.data.vertices[0].co = original_vertex

    marker = object()
    texture_module._ACTIVE_RUNS.append(marker)
    try:
        assert not texture_module.UVMAPPING_OT_analyze_references.poll(bpy.context)
        assert not texture_module.UVMAPPING_OT_generate_turnaround.poll(bpy.context)
    finally:
        texture_module._ACTIVE_RUNS.remove(marker)

    # Blender는 execute()가 반환하면 Operator RNA를 해제하므로, 진행 중 작업 상태는
    # 연산자 밖에 있어야 결과를 회수할 수 있다.
    completed_process = subprocess.Popen(
        (
            bpy.app.binary_path,
            "--background",
            "--factory-startup",
            "--python-expr",
            "pass",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    completed_process.wait(timeout=180)
    run_dir = Path(tempfile.mkdtemp(prefix="uvmapping-run-test-"))
    request_path = run_dir / "request.json"
    response_path = run_dir / "response.json"
    request_path.write_text("{}", encoding="utf-8")
    response_path.write_text(
        json.dumps({"ok": True, "text": "회수된 결과"}), encoding="utf-8"
    )
    collected = []
    finished_run = texture_module._TextureRun(
        completed_process,
        request_path,
        response_path,
        bpy.context.scene.as_pointer(),
        {"probe": "payload"},
        lambda scene, value, payload: collected.append((value["text"], payload["probe"])),
    )
    texture_module._ACTIVE_RUNS.append(finished_run)
    texture_module._ACTIVE_PROCESSES.add(completed_process)
    texture_module._ACTIVE_JOB_DIRS.add(run_dir)
    assert finished_run.poll_process() is None
    assert collected == [("회수된 결과", "payload")]
    assert not run_dir.exists(), "작업 디렉터리를 정리하지 못했습니다."
    assert not texture_module._ACTIVE_RUNS
    assert not texture_module._ACTIVE_PROCESSES
    assert not texture_module._ACTIVE_JOB_DIRS

    def pending_timer():
        return 30.0

    bpy.app.timers.register(pending_timer, first_interval=30.0)
    sleeping_process = subprocess.Popen(
        (
            bpy.app.binary_path,
            "--background",
            "--factory-startup",
            "--python-expr",
            "import time; time.sleep(30)",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pending_dir = Path(tempfile.mkdtemp(prefix="uvmapping-shutdown-test-"))
    pending_request = pending_dir / "request.json"
    pending_request.write_text("{}", encoding="utf-8")
    pending_run = texture_module._TextureRun(
        sleeping_process,
        pending_request,
        pending_dir / "response.json",
        bpy.context.scene.as_pointer(),
        {},
        lambda *_arguments: None,
    )
    pending_run.timer = pending_timer
    texture_module._ACTIVE_RUNS.append(pending_run)
    texture_module._ACTIVE_TIMERS.add(pending_timer)
    texture_module._ACTIVE_PROCESSES.add(sleeping_process)
    texture_module._ACTIVE_JOB_DIRS.add(pending_dir)
    texture_module.shutdown()
    assert not bpy.app.timers.is_registered(pending_timer)
    assert sleeping_process.poll() is not None
    assert not pending_dir.exists()
    assert not texture_module._ACTIVE_RUNS
    assert not texture_module._ACTIVE_TIMERS
    assert not texture_module._ACTIVE_PROCESSES
    assert not texture_module._ACTIVE_JOB_DIRS
    print("[texture] 작업 상태 분리와 shutdown 정리 통과")

    reference = settings.reference_images.add()
    reference.path = str(Path(__file__))
    reference.label = "분석 무효화 테스트"
    settings.reference_image_index = 0
    settings.texture_analysis_json = '{"object_summary":"이전 분석"}'
    settings.texture_analysis_reference_hash = "stale"
    assert bpy.ops.uvmapping.remove_reference_image() == {"FINISHED"}
    assert settings.texture_analysis_json == ""
    assert settings.texture_analysis_reference_hash == ""

    scene = bpy.context.scene
    saved_render = (
        scene.camera,
        scene.render.engine,
        scene.render.filepath,
        scene.render.resolution_x,
        scene.render.resolution_y,
        scene.render.resolution_percentage,
        scene.render.film_transparent,
        scene.render.image_settings.file_format,
        scene.display.shading.light,
        scene.display.shading.color_type,
        tuple(scene.display.shading.single_color),
        scene.display.shading.background_type,
        tuple(scene.display.shading.background_color),
        scene.view_settings.view_transform,
        scene.view_settings.look,
    )
    view_paths = texture_module._render_model_views(bpy.context, (cube,))
    assert len(view_paths) == 3 and all(path.is_file() for path in view_paths)
    assert saved_render == (
        scene.camera,
        scene.render.engine,
        scene.render.filepath,
        scene.render.resolution_x,
        scene.render.resolution_y,
        scene.render.resolution_percentage,
        scene.render.film_transparent,
        scene.render.image_settings.file_format,
        scene.display.shading.light,
        scene.display.shading.color_type,
        tuple(scene.display.shading.single_color),
        scene.display.shading.background_type,
        tuple(scene.display.shading.background_color),
        scene.view_settings.view_transform,
        scene.view_settings.look,
    ), "모델 캡처가 Scene 렌더 설정을 복구하지 못했습니다."
    assert bpy.data.objects.get("UVMapping AI 임시 카메라") is None

    sheet_path = view_paths[0].parent / "test_contact_sheet.png"
    texture_module._join_horizontal(view_paths, sheet_path)
    assert sheet_path.is_file()
    assert texture_module.validate_reference_image_path(sheet_path)[1] == "image/png"
    # contact sheet는 생성 요청과 같은 21:9여야 AI가 열 배치를 그대로 따라 그린다.
    sheet_width = texture_module.MODEL_CAPTURE_RESOLUTION * 3
    sheet_height = round(sheet_width / texture_module.CONTACT_SHEET_ASPECT)
    sheet = bpy.data.images.load(str(sheet_path), check_existing=False)
    try:
        assert tuple(sheet.size) == (sheet_width, sheet_height), tuple(sheet.size)
    finally:
        bpy.data.images.remove(sheet)

    crop_paths = texture_module._crop_turnaround(sheet_path)
    assert len(crop_paths) == 3 and all(path.is_file() for path in crop_paths)
    for crop_path in crop_paths:
        crop = bpy.data.images.load(str(crop_path), check_existing=False)
        try:
            assert tuple(crop.size) == (texture_module.MODEL_CAPTURE_RESOLUTION, sheet_height)
        finally:
            bpy.data.images.remove(crop)

    bake_module = importlib.import_module(f"{MODULE_NAME}.uvmapping.texture_bake")
    with tempfile.TemporaryDirectory(prefix="uvmapping-bake-test-") as temporary_dir:
        bake_dir = Path(temporary_dir)
        solid_paths = {}
        for name, color in (
            ("front", (230, 35, 25, 255)),
            ("right", (35, 210, 55, 255)),
            ("back", (30, 65, 230, 255)),
        ):
            path = bake_dir / f"{name}.png"
            path.write_bytes(bake_module.encode_srgb_png(bytes(color) * (64 * 64), 64, 64))
            solid_paths[name] = path

        gray_path = bake_dir / "gray_midpoint.png"
        gray_path.write_bytes(
            bake_module.encode_srgb_png(bytes((128, 128, 128, 255)) * (4 * 4), 4, 4)
        )
        gray_sources, gray_images = bake_module._load_raster_sources(
            {name: gray_path for name in ("FRONT", "RIGHT", "BACK")}
        )
        try:
            assert abs(gray_sources["FRONT"].pixels[0] - 0.2158605) < 1.0e-5
        finally:
            for image in gray_images:
                bpy.data.images.remove(image)

        # 애드온이 UV를 만들지 않으므로 큐브의 기본 UV에서 계약을 그대로 생성한다.
        settings.texture_resolution = "256"
        settings.padding_pixels = 2
        job_module = importlib.import_module(f"{MODULE_NAME}.uvmapping.texture_job")
        texture_job = job_module.ensure_texture_jobs((cube,), settings)[0]
        assert texture_job["uv_layer_name"] == cube.data.uv_layers.active.name
        assert texture_job["settings"]["uv_source"] == "USER_AUTHORED"
        turnaround_path = bake_dir / "turnaround.png"
        turnaround_path.write_bytes(sheet_path.read_bytes())
        projection = texture_module._projection_contract(bpy.context, (cube,))
        source_job = {
            "object_name": cube.name,
            **{
                key: texture_job.get(key, "")
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
        design_state = {
            "schema_version": "1.1",
            "status": "TURNAROUND_READY",
            "target_objects": [cube.name],
            "source_jobs": [source_job],
            "projection": projection,
            "turnaround_path": str(turnaround_path),
            "turnaround_sha256": hashlib.sha256(turnaround_path.read_bytes()).hexdigest(),
            "views": {name: str(path) for name, path in solid_paths.items()},
            "view_sha256": {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in solid_paths.items()
            },
        }
        cube["uvmapping_texture_design_state"] = json.dumps(design_state)
        assert texture_module.can_bake_diffuse(bpy.context)
        assert bpy.ops.uvmapping.bake_diffuse() == {"FINISHED"}
        diffuse_path = Path(settings.texture_diffuse_path)
        assert diffuse_path.is_file()
        applied_state = json.loads(cube["uvmapping_texture_design_state"])
        assert applied_state["status"] == "ALBEDO_APPLIED"
        assert applied_state["albedo_sha256"] == hashlib.sha256(diffuse_path.read_bytes()).hexdigest()
        baked_image = next(
            image
            for image in bpy.data.images
            if image.get("uvmapping_output_path") == str(diffuse_path.resolve())
        )
        assert tuple(baked_image.size) == (256, 256)
        assert baked_image.colorspace_settings.name == "sRGB"
        baked_material = next(
            material
            for material in bpy.data.materials
            if material.get("uvmapping_output_path") == str(diffuse_path.resolve())
        )
        principled = next(
            node for node in baked_material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        )
        assert principled.inputs["Base Color"].is_linked
        material_count = len(bpy.data.materials)
        image_count = len(bpy.data.images)
        assert bpy.ops.uvmapping.bake_diffuse() == {"FINISHED"}
        assert Path(settings.texture_diffuse_path) == diffuse_path
        assert len(bpy.data.materials) == material_count
        assert len(bpy.data.images) == image_count

        # topology를 바꾸는 Modifier가 있어도 평가 Mesh UV로 베이크되어야 한다.
        cube.modifiers.new("Subdivision", "SUBSURF")
        job_module.ensure_texture_jobs((cube,), settings)
        modifier_state = dict(design_state)
        modifier_state["projection"] = texture_module._projection_contract(bpy.context, (cube,))
        modifier_state.pop("albedo_path", None)
        cube["uvmapping_texture_design_state"] = json.dumps(modifier_state)
        assert bpy.ops.uvmapping.bake_diffuse() == {"FINISHED"}, settings.texture_status
        modifier_path = Path(settings.texture_diffuse_path)
        assert modifier_path.is_file()
        modifier_applied = json.loads(cube["uvmapping_texture_design_state"])
        assert modifier_applied["status"] == "ALBEDO_APPLIED"
        assert modifier_applied["bake_stats"]["outside_atlas_triangles"] == 0
        assert modifier_applied["bake_stats"]["triangle_count"] > 12
        cube.modifiers.remove(cube.modifiers["Subdivision"])

    worker_path = Path(texture_module.__file__).with_name("texture_worker.py")
    with tempfile.TemporaryDirectory(prefix="uvmapping-worker-test-") as temporary_dir:
        request_path = Path(temporary_dir) / "request.json"
        response_path = Path(temporary_dir) / "response.json"
        request_path.write_text(
            json.dumps({"action": "unsupported", "image_paths": []}),
            encoding="utf-8",
        )
        completed = subprocess.run(
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
            input=b"test-key-via-stdin\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0
        worker_result = json.loads(response_path.read_text(encoding="utf-8"))
        assert worker_result["ok"] is False
        assert "지원하지 않는 작업" in worker_result["error"]

    # Edit Mode는 막지 않고 Object Mode로 자동 전환한 뒤 진행해야 한다.
    _clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    edit_cube = bpy.context.object
    bpy.ops.object.mode_set(mode="EDIT")
    assert texture_module.non_object_mode_names(bpy.context) == (edit_cube.name,)
    assert texture_module.UVMAPPING_OT_generate_turnaround.poll(bpy.context)
    assert texture_module._validated_texture_targets(bpy.context) == (edit_cube,)
    assert edit_cube.mode == "OBJECT", "Object Mode 자동 전환이 동작하지 않았습니다."

    # 등록한 대상 객체가 있으면 선택과 무관하게 그 객체만 대상으로 삼는다.
    _clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(-3.0, 0.0, 0.0))
    registered_cube = bpy.context.object
    registered_cube.name = "등록 대상"
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(3.0, 0.0, 0.0))
    other_cube = bpy.context.object
    other_cube.name = "선택만 된 객체"
    registered_cube.select_set(True)
    other_cube.select_set(True)
    assert set(texture_module.texture_targets(bpy.context)) == {registered_cube, other_cube}
    entry = settings.target_objects.add()
    entry.object = registered_cube
    try:
        assert texture_module.registered_targets(bpy.context) == (registered_cube,)
        assert texture_module.texture_targets(bpy.context) == (registered_cube,)
        bpy.ops.object.select_all(action="DESELECT")
        assert texture_module.texture_targets(bpy.context) == (registered_cube,)
        assert texture_module.UVMAPPING_OT_generate_turnaround.poll(bpy.context)
        assert texture_module._validated_texture_targets(bpy.context) == (registered_cube,)
        assert bpy.ops.uvmapping.clear_target_objects() == {"FINISHED"}
        assert not settings.target_objects
    finally:
        settings.target_objects.clear()

    # 파일 선택기 경로: ImportHelper가 주지 않는 directory까지 직접 다뤄야 한다.
    _clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    settings.reference_images.clear()
    with tempfile.TemporaryDirectory(prefix="uvmapping-reference-add-") as temporary_dir:
        reference_dir = Path(temporary_dir)
        reference_names = ("ref_a.png", "ref_b.png")
        for name in reference_names:
            (reference_dir / name).write_bytes(
                bake_module.encode_srgb_png(bytes((200, 120, 60, 255) * 4), 2, 2)
            )
        assert bpy.ops.uvmapping.add_reference_images(
            directory=str(reference_dir),
            files=[{"name": name} for name in reference_names],
        ) == {"FINISHED"}
        assert len(settings.reference_images) == 2
        assert {Path(item.path).name for item in settings.reference_images} == set(
            reference_names
        )
        # 다중 선택 없이 filepath 하나만 온 경우도 같은 경로로 처리한다.
        settings.reference_images.clear()
        assert bpy.ops.uvmapping.add_reference_images(
            filepath=str(reference_dir / reference_names[0])
        ) == {"FINISHED"}
        assert len(settings.reference_images) == 1
        settings.reference_images.clear()

    _check_full_pipeline(texture_module, bake_module)

    _clear_scene()
    addon.unregister()
    assert not _operator_registered(), "등록 해제 뒤 AI 연산자가 남아 있습니다."
    print("[texture] 등록, 모델 3면 캡처, contact sheet, 로컬 3분할, 별도 작업자 검사 통과")


if __name__ == "__main__":
    main()
