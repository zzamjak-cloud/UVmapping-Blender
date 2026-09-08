"""격리 Blender에서 AI 텍스처 UI와 로컬 이미지 단계를 검사한다."""

from __future__ import annotations

import importlib
import hashlib
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
    texture_module._ACTIVE_OPERATORS.append(marker)
    try:
        assert not texture_module.UVMAPPING_OT_analyze_references.poll(bpy.context)
        assert not texture_module.UVMAPPING_OT_generate_turnaround.poll(bpy.context)
    finally:
        texture_module._ACTIVE_OPERATORS.remove(marker)

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

    class DummyJob:
        _process = sleeping_process
        cleaned = False

        def _cleanup_job_files(self):
            self.cleaned = True

    dummy_job = DummyJob()
    dummy_job._app_timer = pending_timer
    texture_module._ACTIVE_OPERATORS.append(dummy_job)
    texture_module._ACTIVE_PROCESSES.add(sleeping_process)
    texture_module.shutdown()
    assert not bpy.app.timers.is_registered(pending_timer)
    assert sleeping_process.poll() is not None
    assert dummy_job.cleaned
    assert not texture_module._ACTIVE_OPERATORS
    assert not texture_module._ACTIVE_PROCESSES

    # Blender 종료 시 RNA가 먼저 해제된 연산자: 어떤 속성 접근도
    # ReferenceError가 되지만 shutdown은 레지스트리로 전부 정리해야 한다.
    class DeadOperator:
        def __getattribute__(self, name):
            raise ReferenceError("StructRNA of type X has been removed")

    def dead_timer():
        return 30.0

    bpy.app.timers.register(dead_timer, first_interval=30.0)
    dead_process = subprocess.Popen(
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
    texture_module._ACTIVE_OPERATORS.append(DeadOperator())
    texture_module._ACTIVE_TIMERS.add(dead_timer)
    texture_module._ACTIVE_PROCESSES.add(dead_process)
    texture_module.shutdown()
    assert not bpy.app.timers.is_registered(dead_timer)
    assert dead_process.poll() is not None
    assert not texture_module._ACTIVE_OPERATORS
    assert not texture_module._ACTIVE_TIMERS
    assert not texture_module._ACTIVE_PROCESSES
    print("[texture] RNA 해제 후 shutdown 정리 통과")

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
    ), "모델 캡처가 Scene 렌더 설정을 복구하지 못했습니다."
    assert bpy.data.objects.get("UVMapping AI 임시 카메라") is None

    sheet_path = view_paths[0].parent / "test_contact_sheet.png"
    texture_module._join_horizontal(view_paths, sheet_path)
    assert sheet_path.is_file()
    assert texture_module.validate_reference_image_path(sheet_path)[1] == "image/png"
    sheet = bpy.data.images.load(str(sheet_path), check_existing=False)
    try:
        assert tuple(sheet.size) == (1536, 512)
    finally:
        bpy.data.images.remove(sheet)

    crop_paths = texture_module._crop_turnaround(sheet_path)
    assert len(crop_paths) == 3 and all(path.is_file() for path in crop_paths)
    for crop_path in crop_paths:
        crop = bpy.data.images.load(str(crop_path), check_existing=False)
        try:
            assert tuple(crop.size) == (512, 512)
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

    _clear_scene()
    addon.unregister()
    assert not _operator_registered(), "등록 해제 뒤 AI 연산자가 남아 있습니다."
    print("[texture] 등록, 모델 3면 캡처, contact sheet, 로컬 3분할, 별도 작업자 검사 통과")


if __name__ == "__main__":
    main()
