"""UV 자동 언랩 설정 속성."""

import time

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, PropertyGroup


def addon_module_id() -> str:
    """현재 설치 방식에서 Blender가 사용하는 Extension 루트 모듈 ID를 반환한다."""

    package_name = __package__ or "uvmapping"
    package_suffix = ".uvmapping"
    if package_name.endswith(package_suffix):
        return package_name[: -len(package_suffix)]
    if package_name == "uvmapping":
        return "uvmapping_blender"
    return package_name


def get_addon_preferences(context):
    """현재 사용자에게 저장된 애드온 환경설정을 반환한다."""

    preferences = getattr(context, "preferences", None)
    addons = getattr(preferences, "addons", None)
    if addons is None:
        return None
    addon = addons.get(addon_module_id())
    return getattr(addon, "preferences", None) if addon is not None else None


class UVMAPPING_AP_preferences(AddonPreferences):
    """Blender 사용자 환경설정에만 저장되는 AI Provider 자격 증명."""

    bl_idname = addon_module_id()

    gemini_api_key: StringProperty(
        name="Gemini API 키",
        description="Gemini 참조 분석과 이미지 생성에 사용할 개인 API 키입니다",
        default="",
        subtype="PASSWORD",
    )
    openai_api_key: StringProperty(
        name="OpenAI API 키",
        description="OpenAI 참조 분석과 이미지 생성에 사용할 개인 API 키입니다",
        default="",
        subtype="PASSWORD",
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "gemini_api_key")
        layout.prop(self, "openai_api_key")
        save_box = layout.box()
        save_box.label(text="API 키를 입력한 뒤 반드시 아래 버튼으로 저장하세요.", icon="INFO")
        save_box.operator("wm.save_userpref", text="API 키 저장", icon="FILE_TICK")
        save_box.label(text="저장 후 새 Blender 프로세스에서도 키가 유지됩니다.")
        warning = layout.box()
        warning.label(text="API 키는 이 컴퓨터의 Blender 사용자 환경설정에 저장됩니다.", icon="INFO")
        warning.label(text="공용 컴퓨터에서는 키를 입력하거나 저장하지 마세요.")


_AUTO_REPACK_STATE = {"deadline": 0.0, "scheduled": False}


def _auto_repack_tick() -> float | None:
    """디바운스가 끝나면 자동 언랩 결과를 현재 설정으로 재배치한다."""

    remaining = _AUTO_REPACK_STATE["deadline"] - time.monotonic()
    if remaining > 0.0:
        return remaining
    _AUTO_REPACK_STATE["scheduled"] = False
    # Edit Mode 등 다른 작업 중에는 모드를 바꾸지 않도록 건너뛴다.
    if getattr(bpy.context, "mode", "OBJECT") != "OBJECT":
        return None
    try:
        if bpy.ops.uvmapping.repack_uvs.poll():
            bpy.ops.uvmapping.repack_uvs()
    except Exception:
        pass
    return None


def _request_auto_repack(settings, _context) -> None:
    """패딩류 설정 변경을 하나로 묶어 자동 UV 재배치를 예약한다."""

    if bpy.app.background or not getattr(settings, "auto_repack", False):
        return
    _AUTO_REPACK_STATE["deadline"] = time.monotonic() + 0.35
    if not _AUTO_REPACK_STATE["scheduled"]:
        _AUTO_REPACK_STATE["scheduled"] = True
        bpy.app.timers.register(_auto_repack_tick, first_interval=0.35)


def _sync_island_margin(settings, context) -> None:
    """사용자용 픽셀 여백을 기존 연산자가 읽는 UV fraction으로 변환한다."""

    resolution = max(1, int(settings.texture_resolution))
    settings.island_margin = settings.padding_pixels / resolution
    _request_auto_repack(settings, context)


class UVMAPPING_PG_reference_image(PropertyGroup):
    """AI가 스타일과 표면 특징을 읽을 참조 이미지 한 장."""

    path: StringProperty(
        name="이미지 경로",
        subtype="FILE_PATH",
    )
    label: StringProperty(
        name="표시 이름",
        default="참조 이미지",
    )


class UVMAPPING_PG_settings(PropertyGroup):
    """씬에 저장되는 자동 언랩 설정."""

    preset: EnumProperty(
        name="프리셋",
        description="메시 유형에 맞는 Seam 분석 가중치를 선택합니다",
        items=(
            ("AUTO", "자동", "이면각·Sharp·재질 경계를 분석해 메시별로 프리셋을 고릅니다"),
            ("ORGANIC", "Organic", "부드러운 곡률과 오목한 영역을 우선합니다"),
            ("BALANCED", "Balanced", "유기체와 하드서페이스에 균형 잡힌 설정입니다"),
            ("HARD_SURFACE", "Hard Surface", "Sharp Edge와 재질 경계를 강하게 반영합니다"),
        ),
        default="AUTO",
    )
    quality_level: EnumProperty(
        name="품질 단계",
        description="비교할 자동 Seam 후보 수를 선택합니다",
        items=(
            ("AUTO", "자동", "메시 크기에 맞춰 비교할 후보 수를 고릅니다"),
            ("FAST", "빠르게", "기본 후보 한 개만 평가합니다"),
            ("BALANCED", "균형", "최대 세 후보를 비교합니다"),
            ("QUALITY", "품질 우선", "최대 다섯 후보를 비교합니다"),
        ),
        default="AUTO",
    )
    generate_texture_job: BoolProperty(
        name="TextureJob 생성",
        description="AI 텍스처 단계가 사용할 품질과 UV 구조 계약을 객체 속성에 저장합니다",
        default=True,
    )
    seam_policy: EnumProperty(
        name="기존 Seam",
        description="기존 Seam을 자동 분석 결과와 합칠지 결정합니다",
        items=(
            ("PRESERVE", "보존", "기존 Seam을 유지하고 새 Seam을 추가합니다"),
            ("REPLACE", "교체", "기존 Seam을 지우고 분석 결과만 적용합니다"),
        ),
        default="PRESERVE",
    )
    create_new_uv_layer: BoolProperty(
        name="새 UV 레이어 생성",
        description="기존 UV를 덮어쓰지 않고 새 UV 레이어에 결과를 만듭니다",
        default=True,
    )
    uv_layer_name: StringProperty(
        name="UV 레이어 이름",
        description="새로 만들거나 사용할 UV 레이어 이름입니다",
        default="AutoUV",
    )
    unwrap_iterations: IntProperty(
        name="최소 스트레치 반복",
        description="Minimum Stretch 언랩의 최적화 반복 횟수입니다",
        default=10,
        min=1,
        max=10000,
    )
    texture_resolution: EnumProperty(
        name="텍스처 크기",
        description="UV를 배치할 정사각형 텍스처의 가로세로 픽셀 크기입니다",
        items=(
            ("256", "256 px", "256 px 텍스처를 대상으로 합니다"),
            ("512", "512 px", "512 px 텍스처를 대상으로 합니다"),
            ("1024", "1024 px", "1K 텍스처를 대상으로 합니다"),
            ("2048", "2048 px", "2K 텍스처를 대상으로 합니다"),
            ("4096", "4096 px", "4K 텍스처를 대상으로 합니다"),
            ("8192", "8192 px", "8K 텍스처를 대상으로 합니다"),
        ),
        default="2048",
        update=_sync_island_margin,
    )
    padding_pixels: IntProperty(
        name="UV 패딩",
        description="각 UV 조각 가장자리에 확보할 여백을 픽셀 단위로 정합니다",
        default=16,
        min=0,
        max=256,
        subtype="PIXEL",
        update=_sync_island_margin,
    )
    pack_shared_atlas: BoolProperty(
        name="선택 객체를 한 장에 배치",
        description="선택한 모든 Mesh의 UV를 겹치지 않게 한 장의 텍스처 공간에 함께 배치합니다",
        default=True,
        update=_request_auto_repack,
    )
    auto_repack: BoolProperty(
        name="설정 변경 시 자동 재배치",
        description="패딩·텍스처 크기를 바꾸면 생성된 UV를 즉시 다시 배치합니다",
        default=True,
    )
    island_margin: FloatProperty(
        name="내부 UV 여백",
        description="픽셀 패딩에서 자동 환산되는 내부 호환용 UV fraction입니다",
        default=0.0078125,
        min=0.0,
        max=0.5,
        precision=4,
        subtype="FACTOR",
        options={"HIDDEN"},
    )
    fill_holes: BoolProperty(
        name="구멍 채움 고려",
        description="언랩 계산에서 내부 구멍을 채운 것으로 처리합니다",
        default=True,
    )
    correct_aspect: BoolProperty(
        name="이미지 비율 보정",
        description="활성 이미지의 가로세로 비율을 UV 계산에 반영합니다",
        default=True,
    )
    show_advanced: BoolProperty(
        name="고급 설정",
        default=False,
    )
    use_custom_analysis: BoolProperty(
        name="프리셋 세부 조정",
        description="프리셋의 일부 분석 값을 아래 값으로 덮어씁니다",
        default=False,
    )
    angle_weight: FloatProperty(
        name="각도 가중치",
        default=1.0,
        min=0.0,
        max=10.0,
    )
    concave_multiplier: FloatProperty(
        name="오목부 배수",
        default=1.35,
        min=0.0,
        max=10.0,
    )
    sharp_weight: FloatProperty(
        name="Sharp 가중치",
        default=1.5,
        min=0.0,
        max=10.0,
    )
    material_weight: FloatProperty(
        name="재질 경계 가중치",
        default=1.0,
        min=0.0,
        max=10.0,
    )
    seam_threshold: FloatProperty(
        name="Seam 임계값",
        description="점수가 이 값 이상인 Edge를 Seam 후보로 사용합니다",
        default=0.68,
        min=0.0,
        max=1.0,
    )
    min_chart_faces: IntProperty(
        name="최소 차트 면 수",
        description="지나치게 작은 UV 아일랜드 생성을 억제합니다",
        default=6,
        min=1,
        max=10000,
    )
    ensure_cut_paths: BoolProperty(
        name="폐곡면 절단 경로 보장",
        description="닫힌 메시가 펼쳐지도록 추가 절단 경로를 만듭니다",
        default=True,
    )
    connect_boundary_loops: BoolProperty(
        name="경계 루프 연결",
        description="여러 경계 루프를 연결해 펼치기 쉬운 차트를 만듭니다",
        default=True,
    )
    last_result: StringProperty(
        name="최근 분석",
        default="",
        options={"SKIP_SAVE"},
    )

    reference_images: CollectionProperty(
        name="참조 이미지",
        type=UVMAPPING_PG_reference_image,
    )
    reference_image_index: IntProperty(
        name="선택 참조 이미지",
        default=0,
        min=0,
    )
    texture_user_prompt: StringProperty(
        name="추가 지시",
        description="참조 이미지에 없는 요구만 짧게 입력합니다",
        default="",
    )
    texture_image_provider: EnumProperty(
        name="이미지 생성 모델",
        description="참조 분석과 한 장짜리 3면도 생성에 사용할 AI 모델을 선택합니다",
        items=(
            (
                "GEMINI",
                "Nano Banana Pro",
                "Gemini 분석과 Nano Banana Pro로 3면도를 생성합니다",
            ),
            (
                "OPENAI",
                "GPT-Image-2 (덕테이프)",
                "GPT-5.6 분석과 GPT-Image-2로 3면도를 생성합니다",
            ),
        ),
        default="GEMINI",
    )
    texture_analysis_model: StringProperty(
        name="분석 모델",
        default="gemini-3.7-flash",
    )
    texture_image_model: StringProperty(
        name="이미지 모델",
        default="gemini-3-pro-image",
    )
    texture_openai_analysis_model: StringProperty(
        name="OpenAI 분석 모델",
        default="gpt-5.6",
    )
    texture_openai_image_model: StringProperty(
        name="OpenAI 이미지 모델",
        default="gpt-image-2",
    )
    show_texture_advanced: BoolProperty(
        name="AI 고급 설정",
        default=False,
    )
    texture_analysis_json: StringProperty(
        name="참조 분석 JSON",
        default="",
        options={"HIDDEN"},
    )
    texture_analysis_reference_hash: StringProperty(
        name="분석 참조 해시",
        default="",
        options={"HIDDEN"},
    )
    texture_output_path: StringProperty(
        name="3면도 출력",
        default="",
        subtype="FILE_PATH",
    )
    texture_diffuse_path: StringProperty(
        name="Diffuse/Albedo 출력",
        default="",
        subtype="FILE_PATH",
    )
    texture_status: StringProperty(
        name="AI 텍스처 상태",
        default="참조 이미지를 추가해 주세요",
        options={"SKIP_SAVE"},
    )
__all__ = (
    "UVMAPPING_AP_preferences",
    "UVMAPPING_PG_reference_image",
    "UVMAPPING_PG_settings",
    "addon_module_id",
    "get_addon_preferences",
)
