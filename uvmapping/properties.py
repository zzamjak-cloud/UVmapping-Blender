"""AI 텍스처링 설정 속성."""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, PropertyGroup

from .texture_bake import (
    DEFAULT_TRANSITION_BAND_DEGREES,
    MAX_TRANSITION_BAND_DEGREES,
    MIN_TRANSITION_BAND_DEGREES,
)
from .texture_presets import (
    AUTO_IMAGE_QUALITY,
    DEFAULT_QUALITY_PRESET,
    IMAGE_QUALITY_ITEMS,
    QUALITY_PRESET_FIELDS,
    QUALITY_PRESET_ITEMS,
    TEXTURE_RESOLUTION_OPTIONS,
    quality_preset_after_change,
    quality_preset_values,
)


# 투영 블렌딩 기본값. 패널 기본값과 상태에 값이 없을 때의 대체값을 한곳에서 정한다.
DEFAULT_DOMINANT_VIEW_BLEND = True


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
GPT_IMAGE_25_SUNBURST_MODELS = ("openai/gpt-5.6-sol", "openai/gpt-image-2.5-sunburst")
GPT_IMAGE_25_FLARE_MODELS = ("openai/gpt-5.6-sol", "openai/gpt-image-2.5-flare")
MODEL_PRESETS = {
    "NANO_BANANA_PRO": NANO_BANANA_PRO_MODELS,
    "GPT_IMAGE": GPT_IMAGE_MODELS,
    "GPT_IMAGE_25_SUNBURST": GPT_IMAGE_25_SUNBURST_MODELS,
    "GPT_IMAGE_25_FLARE": GPT_IMAGE_25_FLARE_MODELS,
}
# 품질 프리셋과 개별 프로퍼티의 update 콜백이 서로를 다시 부르는 것을 막는 단일 가드.
_APPLYING_QUALITY_PRESET = False


def _apply_model_preset(settings, _context) -> None:
    """프리셋을 바꾸면 두 모델 식별자를 해당 조합으로 덮어쓴다."""

    models = MODEL_PRESETS.get(str(settings.texture_model_preset))
    if models is None:
        return
    settings.texture_analysis_model, settings.texture_image_model = models


def quality_values_from(settings) -> dict:
    """프리셋 비교에 쓰는 개별 프로퍼티 현재 값."""

    return {name: getattr(settings, name) for name in QUALITY_PRESET_FIELDS}


def _apply_quality_preset(settings, _context) -> None:
    """품질 프리셋을 고르면 표대로 개별 프로퍼티를 덮어쓴다. CUSTOM은 현재 값을 유지한다."""

    global _APPLYING_QUALITY_PRESET
    if _APPLYING_QUALITY_PRESET:
        return
    values = quality_preset_values(str(settings.texture_quality_preset))
    if values is None:
        return
    _APPLYING_QUALITY_PRESET = True
    try:
        for name, value in values.items():
            setattr(settings, name, value)
    finally:
        _APPLYING_QUALITY_PRESET = False


def _mark_quality_custom(settings, _context) -> None:
    """고급 설정에서 개별 값을 직접 바꾸면 프리셋 표시를 CUSTOM으로 내린다."""

    global _APPLYING_QUALITY_PRESET
    if _APPLYING_QUALITY_PRESET:
        return
    resolved = quality_preset_after_change(
        str(settings.texture_quality_preset), quality_values_from(settings)
    )
    if resolved == str(settings.texture_quality_preset):
        return
    _APPLYING_QUALITY_PRESET = True
    try:
        settings.texture_quality_preset = resolved
    finally:
        _APPLYING_QUALITY_PRESET = False


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

    texture_quality_preset: EnumProperty(
        name="품질",
        description=(
            "시점 구성·생성 이미지 크기·텍스처 크기·다시 그리기 횟수·적용 후 검증을 한 번에 정합니다. "
            "고급 설정에서 이 값들을 직접 바꾸면 사용자 지정으로 바뀝니다"
        ),
        items=QUALITY_PRESET_ITEMS,
        default=DEFAULT_QUALITY_PRESET,
        update=_apply_quality_preset,
    )
    texture_resolution: EnumProperty(
        name="텍스처 크기",
        description="AI 텍스처를 구울 정사각형 이미지의 가로세로 픽셀 크기입니다",
        items=TEXTURE_RESOLUTION_OPTIONS,
        default="1024",
        update=_mark_quality_custom,
    )
    padding_pixels: IntProperty(
        name="UV 패딩",
        description="베이크 결과에서 UV 조각 바깥으로 번지게 할 여백을 픽셀 단위로 정합니다",
        default=16,
        min=0,
        max=256,
        subtype="PIXEL",
    )
    turnaround_layout: EnumProperty(
        name="시점 구성",
        description=(
            "생성할 시점 구성입니다. 6면도는 상·하·좌 시점까지 실제 그림으로 받아 투영합니다. "
            "QUAD는 같은 6면을 캔버스 4장으로 나눠 받으므로 호출이 4회입니다"
        ),
        items=(
            ("THREE", "3면도 · 21:9", "FRONT, RIGHT, BACK 3열 · 21:9 · 1회 호출"),
            (
                "SIX",
                "6면도 · 3:2",
                "FRONT, RIGHT, BACK / LEFT, TOP, BOTTOM 3×2 · 3:2 · 1회 호출, 상·하·좌 포함",
            ),
            (
                "QUAD",
                "6면도 · 4회 호출",
                "정면 단독 · 뒷면 단독 · 좌우 2칸 · 상하 2칸으로 나눠 4회 호출 · "
                "한 캔버스에 6시점을 몰아넣지 않아 구도가 안정되고 정면·뒷면이 캔버스를 통째로 씁니다 · "
                "정면을 먼저 만들어 색 기준으로 삼는 2라운드 실행 · 비용 4배",
            ),
        ),
        default="SIX",
        update=_mark_quality_custom,
    )
    generation_mode: EnumProperty(
        name="생성 방식",
        description=(
            "SINGLE은 모든 시점을 한 장에 담아 한 번만 호출합니다. "
            "SEQUENTIAL은 시점을 하나씩 생성하며 앞 시점의 투영 결과를 다음 가이드로 넘겨 "
            "시점 간 색·무늬 일관성을 높이지만, 시점 수만큼 호출하므로 비용이 N배입니다"
        ),
        items=(
            ("SINGLE", "한 번 호출 · 다면도 한 장", "모든 시점을 한 캔버스에 담아 1회 호출"),
            (
                "SEQUENTIAL",
                "순차 인페인팅 · 시점 수만큼 호출",
                "시점마다 1회씩 호출해 앞 시점의 채색을 유지한 채 빈 곳만 채움 · 비용 N배",
            ),
        ),
        default="SINGLE",
    )
    turnaround_image_size: EnumProperty(
        name="생성 이미지 크기",
        description=(
            "Provider에 요청할 캔버스 해상도입니다. 자동은 6면도·QUAD 2K, 3면도 1K를 씁니다. "
            "한 캔버스에 담는 시점이 적을수록 시점당 픽셀 수가 커지므로 QUAD의 정면·뒷면이 가장 큽니다. "
            "4K는 캔버스마다 비용이 커지므로 직접 선택할 때만 씁니다"
        ),
        items=(
            ("AUTO", "자동", "6면도·QUAD 2K · 3면도 1K · 순차 모드 시점당 1K"),
            ("1K", "1K", "1K 캔버스 · 시점당 해상도가 가장 낮아 QUAD에는 권장하지 않음"),
            ("2K", "2K", "2K 캔버스 · 비용과 시점당 해상도의 기본 절충값"),
            ("4K", "4K", "4K 캔버스 · 시점당 해상도가 가장 높지만 캔버스마다 비용 주의"),
        ),
        default="AUTO",
        update=_mark_quality_custom,
    )
    texture_image_quality: EnumProperty(
        name="품질 단계",
        description=(
            "이미지 크기 대신 품질 단계를 받는 모델(GPT 계열)에 보낼 등급입니다. "
            "이 모델들은 결과 픽셀이 종횡비로 고정되어 1K급이므로 크기 대신 등급으로 정밀도를 올립니다"
        ),
        items=IMAGE_QUALITY_ITEMS,
        default=AUTO_IMAGE_QUALITY,
        update=_mark_quality_custom,
    )
    auto_regenerate_attempts: IntProperty(
        name="형태가 어긋나면 다시 그리기(횟수)",
        description=(
            "생성 직후 시점별 실루엣을 모델과 비교해 내부 구조가 어긋난 시점이 있으면 교정 지시를 붙여 "
            "다시 생성하는 최대 횟수입니다. 재생성마다 OpenRouter 호출과 비용이 추가됩니다. 0이면 경고만 남깁니다"
        ),
        default=1,
        min=0,
        max=2,
        update=_mark_quality_custom,
    )
    verify_after_bake: BoolProperty(
        name="적용 후 검증",
        description=(
            "Diffuse/Albedo 적용 뒤 모델을 각 시점에서 다시 렌더해 생성 그림과 비교한 점수와 "
            "검증 시트(가이드/생성/베이크 렌더)를 남깁니다. 로컬 처리라 추가 비용은 없습니다"
        ),
        default=True,
        update=_mark_quality_custom,
    )
    blend_exponent: FloatProperty(
        name="시점 전이 폭",
        description="시점 경계에서 색을 섞는 폭을 정합니다. 값이 클수록 전이가 좁아져 측면 색이 정면으로 덜 번집니다",
        default=4.0,
        min=2.0,
        max=8.0,
        step=50,
        precision=1,
    )
    dominant_view_blend: BoolProperty(
        name="시점 경계 선명하게",
        description=(
            "픽셀마다 가장 정면으로 보이는 시점 하나를 쓰고 경계 띠에서만 색을 섞습니다. "
            "정합이 어긋난 두 시점이 겹쳐 보이는 이중상을 줄입니다"
        ),
        default=DEFAULT_DOMINANT_VIEW_BLEND,
    )
    transition_band_degrees: FloatProperty(
        name="경계 혼합 폭(도)",
        description=(
            "지배 시점과 입사각 차가 이 각도 안인 시점만 함께 섞습니다. "
            "좁을수록 경계가 선명하고, 넓을수록 전이가 부드럽습니다"
        ),
        default=DEFAULT_TRANSITION_BAND_DEGREES,
        min=MIN_TRANSITION_BAND_DEGREES,
        max=MAX_TRANSITION_BAND_DEGREES,
        step=100,
        precision=1,
    )
    harmonize_view_colors: BoolProperty(
        name="시점 간 색조 보정",
        description="두 시점이 함께 보는 면의 평균색을 비교해 측면·뒷면의 밝기와 색조를 정면 기준으로 맞춥니다",
        default=True,
    )
    silhouette_warp: BoolProperty(
        name="실루엣 행 워프 (실험적)",
        description=(
            "생성된 그림의 실루엣 폭을 모델 투영 실루엣에 행 단위로 맞춥니다. "
            "팔다리가 없는 소품처럼 줄마다 부위가 하나인 형상에서만 켜세요. "
            "캐릭터에서는 몸통 중앙이 흔들릴 수 있습니다"
        ),
        default=False,
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
                "openai/gpt-5.6-sol 분석과 openai/gpt-5.4-image-2 생성 · "
                "GPT 계열은 픽셀이 종횡비로 고정되어 '생성 이미지 크기'가 품질 단계로 전달됩니다",
            ),
            (
                "GPT_IMAGE_25_SUNBURST",
                "GPT Image 2.5 Sunburst (정밀, 느리고 비쌈)",
                "openai/gpt-5.6-sol 분석과 openai/gpt-image-2.5-sunburst(덕테이프 2.5 선버스트) 생성 · "
                "GPT 계열은 픽셀이 종횡비로 고정되어 '생성 이미지 크기'가 품질 단계로 전달됩니다",
            ),
            (
                "GPT_IMAGE_25_FLARE",
                "GPT Image 2.5 Flare (속도 우선)",
                "openai/gpt-5.6-sol 분석과 openai/gpt-image-2.5-flare(덕테이프 2.5 플레어) 생성 · "
                "GPT 계열은 픽셀이 종횡비로 고정되어 '생성 이미지 크기'가 품질 단계로 전달됩니다",
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
    show_texture_expert: BoolProperty(
        name="AI 전문가 설정",
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
    texture_last_error: StringProperty(
        name="마지막 실패 원문",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    texture_last_error_kind: StringProperty(
        name="마지막 실패 분류",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )


__all__ = (
    "DEFAULT_DOMINANT_VIEW_BLEND",
    "GPT_IMAGE_25_FLARE_MODELS",
    "GPT_IMAGE_25_SUNBURST_MODELS",
    "GPT_IMAGE_MODELS",
    "MODEL_PRESETS",
    "NANO_BANANA_PRO_MODELS",
    "UVMAPPING_AP_preferences",
    "UVMAPPING_PG_reference_image",
    "UVMAPPING_PG_settings",
    "UVMAPPING_PG_target_object",
    "addon_module_id",
    "get_addon_preferences",
    "quality_values_from",
)
