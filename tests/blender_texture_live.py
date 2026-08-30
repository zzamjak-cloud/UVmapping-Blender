"""등록된 개인 키로 공급자별 유료 AI 호출과 최종 Albedo 적용을 검증한다.

자동 회귀에는 포함하지 않는다. 각 Provider를 분석 1회, 이미지 생성 1회만
호출하며 실패를 자동 재시도하지 않는다.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import tempfile
import uuid

import bpy


MODULE_NAME = "bl_ext.user_default.uvmapping_blender"


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _write_reference(path: Path, encode_png) -> None:
    width = height = 256
    pixels = bytearray((236, 222, 185, 255) * (width * height))

    def fill(left: int, bottom: int, right: int, top: int, color: tuple[int, ...]):
        for y in range(bottom, top):
            for x in range(left, right):
                offset = (y * width + x) * 4
                pixels[offset : offset + 4] = bytes(color)

    fill(36, 44, 220, 212, (113, 55, 30, 255))
    fill(46, 54, 210, 202, (173, 89, 43, 255))
    fill(46, 112, 210, 126, (92, 42, 27, 255))
    fill(70, 54, 84, 202, (219, 157, 49, 255))
    fill(172, 54, 186, 202, (219, 157, 49, 255))
    fill(116, 105, 140, 135, (235, 184, 61, 255))
    path.write_bytes(encode_png(pixels, width, height))


def main() -> None:
    addon = importlib.import_module(MODULE_NAME)
    properties = importlib.import_module(f"{MODULE_NAME}.uvmapping.properties")
    operators = importlib.import_module(f"{MODULE_NAME}.uvmapping.texture_operators")
    pipeline = importlib.import_module(f"{MODULE_NAME}.uvmapping.texture_pipeline")
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    worker = importlib.import_module("uvmapping.texture_worker")
    bake = importlib.import_module(f"{MODULE_NAME}.uvmapping.texture_bake")

    addon.unregister()
    addon.register()
    preferences = properties.get_addon_preferences(bpy.context)
    keys = {
        "GEMINI": getattr(preferences, "gemini_api_key", "").strip(),
        "OPENAI": getattr(preferences, "openai_api_key", "").strip(),
    }
    missing = [name for name, value in keys.items() if not value]
    if missing:
        raise RuntimeError(
            "개발 프로필 애드온 환경설정에 API 키가 없습니다: " + ", ".join(missing)
        )

    _clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.object
    uv_name = cube.data.uv_layers.active.name
    output_dir = (
        Path(tempfile.gettempdir())
        / "uvmapping_ai"
        / "live_tests"
        / uuid.uuid4().hex
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    reference_path = output_dir / "stylized_crate_reference.png"
    _write_reference(reference_path, bake.encode_srgb_png)

    model_paths = operators._render_model_views(bpy.context, (cube,))
    geometry_path = operators._join_horizontal(
        model_paths, output_dir / "cube_geometry.png"
    )
    for path in model_paths:
        path.unlink(missing_ok=True)

    providers = (
        ("GEMINI", "gemini-3.7-flash", "gemini-3-pro-image"),
        ("OPENAI", "gpt-5.6", "gpt-image-2"),
    )
    failures = []
    for provider, analysis_model, image_model in providers:
        try:
            analysis_result = worker.run_job(
                {
                    "action": "analyze",
                    "provider": provider,
                    "model": analysis_model,
                    "prompt": pipeline.build_reference_analysis_prompt(1),
                    "image_paths": [str(reference_path)],
                },
                keys[provider],
            )
            analysis = pipeline.parse_reference_analysis(analysis_result["text"])
            turnaround_result = worker.run_job(
                {
                    "action": "turnaround",
                    "provider": provider,
                    "model": image_model,
                    "prompt": pipeline.compile_turnaround_prompt(
                        analysis, "밝은 모서리와 과장된 금색 잠금장치"
                    ),
                    "image_paths": [str(geometry_path), str(reference_path)],
                    "output_path": str(output_dir / f"{provider.lower()}_turnaround.png"),
                },
                keys[provider],
            )
            turnaround_path = Path(turnaround_result["output_path"])
            crops = operators._crop_turnaround(turnaround_path)
            albedo_path = output_dir / f"{provider.lower()}_albedo.png"
            result = bake.bake_diffuse(
                bpy.context,
                (cube,),
                crops,
                albedo_path,
                256,
                4,
                uv_layer_names=(uv_name,),
            )
            assert turnaround_path.is_file() and albedo_path.is_file()
            assert result["status"] == "ALBEDO_APPLIED"
            print(
                f"[live] {provider}: analysis=1, image=1, "
                f"filled={result['filled_pixels']}, fallback={result['fallback_pixels']}"
            )
        except Exception as exc:  # 공급자 하나가 실패해도 다른 공급자를 검증한다.
            failures.append(f"{provider}: {type(exc).__name__}: {exc}")
            print(f"[live] {provider}: 실패(재시도 없음) - {type(exc).__name__}: {exc}")

    print(f"[live] 결과 폴더: {output_dir}")
    if failures:
        raise AssertionError(" | ".join(failures))
    print("[live] 공급자 2종 유료 호출과 최종 Albedo 적용 통과")


if __name__ == "__main__":
    main()
