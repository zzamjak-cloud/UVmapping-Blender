"""AI 텍스처링 설정 속성."""

from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
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
        default="2048",
    )
    padding_pixels: IntProperty(
        name="UV 패딩",
        description="베이크 결과에서 UV 조각 바깥으로 번지게 할 여백을 픽셀 단위로 정합니다",
        default=16,
        min=0,
        max=256,
        subtype="PIXEL",
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
