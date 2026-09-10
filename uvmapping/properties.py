"""AI 텍스처링 설정 속성."""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
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
    """Blender 사용자 환경설정에만 저장되는 OpenRouter 자격 증명."""

    bl_idname = addon_module_id()

    openrouter_api_key: StringProperty(
        name="OpenRouter API 키",
        description="참조 분석과 3면도 생성을 모두 OpenRouter 한 곳으로 호출할 개인 API 키입니다",
        default="",
        subtype="PASSWORD",
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "openrouter_api_key")
        issue_box = layout.box()
        issue_box.label(text="키는 openrouter.ai/keys에서 발급합니다.", icon="URL")
        issue_box.operator(
            "wm.url_open", text="OpenRouter 키 발급 페이지 열기", icon="URL"
        ).url = "https://openrouter.ai/keys"
        save_box = layout.box()
        save_box.label(text="API 키를 입력한 뒤 반드시 아래 버튼으로 저장하세요.", icon="INFO")
        save_box.operator("wm.save_userpref", text="API 키 저장", icon="FILE_TICK")
        save_box.label(text="저장 후 새 Blender 프로세스에서도 키가 유지됩니다.")
        warning = layout.box()
        warning.label(text="API 키는 이 컴퓨터의 Blender 사용자 환경설정에 저장됩니다.", icon="INFO")
        warning.label(text="공용 컴퓨터에서는 키를 입력하거나 저장하지 마세요.")


# OpenRouter 모델 식별자는 "제공자/모델" 형식이며 (분석 모델, 이미지 모델) 순서다.
NANO_BANANA_PRO_MODELS = ("google/gemini-3.7-flash", "google/gemini-3-pro-image")
GPT_IMAGE_MODELS = ("openai/gpt-5.6-sol", "openai/gpt-5.4-image-2")
MODEL_PRESETS = {
    "NANO_BANANA_PRO": NANO_BANANA_PRO_MODELS,
    "GPT_IMAGE": GPT_IMAGE_MODELS,
}


def _apply_model_preset(settings, _context) -> None:
    """프리셋을 바꾸면 두 모델 식별자를 해당 조합으로 덮어쓴다."""

    models = MODEL_PRESETS.get(str(settings.texture_model_preset))
    if models is None:
        return
    settings.texture_analysis_model, settings.texture_image_model = models


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


def _is_texturable_mesh(_self, obj) -> bool:
    """대상 목록에는 폴리곤이 있는 Mesh만 등록할 수 있다."""

    return obj.type == "MESH" and obj.data is not None and len(obj.data.polygons) > 0


class UVMAPPING_PG_target_object(PropertyGroup):
    """텍스처를 구울 대상으로 명시 등록한 Mesh 객체."""

    object: PointerProperty(
        name="대상 객체",
        description="이 3면도와 텍스처의 대상이 될 Mesh 객체입니다",
        type=bpy.types.Object,
        poll=_is_texturable_mesh,
    )


class UVMAPPING_PG_settings(PropertyGroup):
    """씬에 저장되는 AI 텍스처링 설정."""

    texture_resolution: EnumProperty(
        name="텍스처 크기",
        description="AI 텍스처를 구울 정사각형 이미지의 가로세로 픽셀 크기입니다",
        items=(
            ("256", "256 px", "256 px 텍스처를 대상으로 합니다"),
            ("512", "512 px", "512 px 텍스처를 대상으로 합니다"),
            ("1024", "1024 px", "1K 텍스처를 대상으로 합니다"),
            ("2048", "2048 px", "2K 텍스처를 대상으로 합니다"),
            ("4096", "4096 px", "4K 텍스처를 대상으로 합니다"),
            ("8192", "8192 px", "8K 텍스처를 대상으로 합니다"),
        ),
        default="1024",
    )
    padding_pixels: IntProperty(
        name="UV 패딩",
        description="베이크 결과에서 UV 조각 바깥으로 번지게 할 여백을 픽셀 단위로 정합니다",
        default=16,
        min=0,
        max=256,
        subtype="PIXEL",
    )
    target_objects: CollectionProperty(
        name="대상 객체",
        type=UVMAPPING_PG_target_object,
    )
    target_object_index: IntProperty(
        name="선택 대상 객체",
        default=0,
        min=0,
    )
    send_reference_images: BoolProperty(
        name="생성에도 참조 이미지 전달",
        description=(
            "3면도 생성 호출에 참조 이미지 원본을 함께 보냅니다. "
            "이미지 모델이 참조의 캐릭터 형상을 그대로 복제해 모델 실루엣을 "
            "무시할 수 있으므로 기본값은 꺼짐이며, 스타일은 분석 결과로 전달됩니다"
        ),
        default=False,
    )
    auto_apply_diffuse: BoolProperty(
        name="생성 후 자동 적용",
        description="3면도 생성이 끝나면 Diffuse/Albedo 베이크와 머티리얼 적용까지 이어서 실행합니다",
        default=True,
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
    texture_model_preset: EnumProperty(
        name="이미지 생성 모델",
        description="OpenRouter에서 사용할 분석·이미지 모델 조합을 선택합니다",
        items=(
            (
                "NANO_BANANA_PRO",
                "Nano Banana Pro",
                "google/gemini-3.7-flash 분석과 google/gemini-3-pro-image 생성",
            ),
            (
                "GPT_IMAGE",
                "GPT Image (덕테이프)",
                "openai/gpt-5.6-sol 분석과 openai/gpt-5.4-image-2 생성",
            ),
        ),
        default="NANO_BANANA_PRO",
        update=_apply_model_preset,
    )
    texture_analysis_model: StringProperty(
        name="분석 모델",
        description="OpenRouter 참조 분석 모델 식별자입니다",
        default=NANO_BANANA_PRO_MODELS[0],
    )
    texture_image_model: StringProperty(
        name="이미지 모델",
        description="OpenRouter 3면도 생성 모델 식별자입니다",
        default=NANO_BANANA_PRO_MODELS[1],
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
    "GPT_IMAGE_MODELS",
    "MODEL_PRESETS",
    "NANO_BANANA_PRO_MODELS",
    "UVMAPPING_AP_preferences",
    "UVMAPPING_PG_reference_image",
    "UVMAPPING_PG_settings",
    "UVMAPPING_PG_target_object",
    "addon_module_id",
    "get_addon_preferences",
)
