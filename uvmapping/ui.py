"""3D View 사이드바 사용자 인터페이스."""

import os
from pathlib import Path

import bpy
from bpy.types import Panel, UIList

from .properties import get_addon_preferences
from .texture_operators import can_bake_diffuse


def _has_texture_target(context) -> bool:
    """폴리곤이 있는 Mesh가 선택되어 있어야 텍스처 단계를 노출한다."""

    candidates = getattr(context, "selected_editable_objects", ())
    return any(
        obj.type == "MESH" and obj.data is not None and len(obj.data.polygons) > 0
        for obj in candidates
    )


def _missing_uv_names(context) -> tuple[str, ...]:
    """활성 UV 맵이 없는 선택 객체 이름을 모은다."""

    names = []
    for obj in getattr(context, "selected_editable_objects", ()):
        if obj.type != "MESH" or obj.data is None or not len(obj.data.polygons):
            continue
        if obj.data.uv_layers.active is None:
            names.append(obj.name)
    return tuple(names)


class UVMAPPING_UL_reference_images(UIList):
    """사용자가 자유롭게 추가한 참조 이미지 목록."""

    def draw_item(
        self,
        _context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_property,
        _index,
    ):
        layout.label(text=Path(item.path).name or "참조 이미지", icon="IMAGE_DATA")


class UVMAPPING_PT_ai_texture(Panel):
    """스타일 참조 분석과 한 장짜리 모델 3면도 생성 패널."""

    bl_label = "AI 손맵 텍스처"
    bl_idname = "UVMAPPING_PT_ai_texture"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UV Mapping"

    @classmethod
    def poll(cls, context):
        return _has_texture_target(context)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.uvmapping_settings

        if not bpy.app.online_access:
            online_box = layout.box()
            online_box.label(text="AI 기능은 온라인 접근이 필요합니다", icon="ERROR")
            if getattr(bpy.app, "online_access_override", False):
                online_box.label(text="--offline-mode로 실행되어 켤 수 없습니다")
            else:
                online_box.prop(
                    context.preferences.system,
                    "use_online_access",
                    text="Allow Online Access 켜기",
                )

        missing_uv = _missing_uv_names(context)
        if missing_uv:
            uv_box = layout.box()
            uv_box.label(text="UV 맵이 없는 객체가 있습니다", icon="ERROR")
            uv_box.label(text=", ".join(missing_uv))
            uv_box.label(text="UV Editing에서 UV를 펼친 뒤 0-1 안에 배치해 주세요")

        model_box = layout.box()
        model_box.label(text="이미지 생성 모델", icon="IMAGE_DATA")
        model_choices = model_box.column(align=True)
        model_choices.prop(settings, "texture_image_provider", expand=True)

        reference_box = layout.box()
        reference_box.label(text="스타일 참조", icon="IMAGE_DATA")
        reference_box.template_list(
            "UVMAPPING_UL_reference_images",
            "",
            settings,
            "reference_images",
            settings,
            "reference_image_index",
            rows=3,
        )
        controls = reference_box.row(align=True)
        controls.operator("uvmapping.add_reference_images", text="추가", icon="ADD")
        controls.operator(
            "uvmapping.paste_reference_image", text="클립보드", icon="PASTEDOWN"
        )
        controls.operator("uvmapping.remove_reference_image", text="제거", icon="REMOVE")
        preferences = get_addon_preferences(context)
        if settings.texture_image_provider == "OPENAI":
            provider_name = "OpenAI"
            api_key = getattr(preferences, "openai_api_key", "") or os.environ.get(
                "OPENAI_API_KEY", ""
            )
        else:
            provider_name = "Gemini"
            api_key = getattr(preferences, "gemini_api_key", "") or os.environ.get(
                "GEMINI_API_KEY", ""
            )
        key_status = reference_box.box()
        if api_key.strip():
            key_status.label(text=f"{provider_name} API 키 사용 가능", icon="CHECKMARK")
        else:
            key_status.label(text=f"{provider_name} API 키가 필요합니다", icon="ERROR")
            key_status.label(text="환경설정 > 애드온에서 API 키를 입력하세요")
            key_status.operator("screen.userpref_show", text="환경설정 열기", icon="PREFERENCES")
        has_references = bool(settings.reference_images)
        analyze = reference_box.row()
        analyze.enabled = has_references
        analyze.operator("uvmapping.analyze_references", icon="VIEWZOOM")
        if not has_references:
            reference_box.label(
                text="참조 없이도 아래 프롬프트만으로 생성할 수 있습니다", icon="INFO"
            )

        prompt_box = layout.box()
        prompt_box.label(text="추가 지시", icon="TEXT")
        prompt_box.prop(settings, "texture_user_prompt", text="")
        prompt_box.operator(
            "uvmapping.edit_texture_prompt", text="한글 프롬프트 입력", icon="TEXT"
        )

        output_box = layout.box()
        output_box.label(text="출력 텍스처", icon="TEXTURE")
        output_box.prop(settings, "texture_resolution")
        output_box.prop(settings, "padding_pixels")

        generate = layout.column()
        generate.scale_y = 1.5
        if has_references:
            # 참조가 있으면 분석 결과가 있어야 스타일 근거가 확정된다.
            generate.enabled = bool(settings.texture_analysis_json)
        else:
            generate.enabled = bool(settings.texture_user_prompt.strip())
        generate.operator("uvmapping.generate_turnaround", icon="RENDER_STILL")

        apply_texture = layout.column()
        apply_texture.scale_y = 1.5
        apply_texture.enabled = can_bake_diffuse(context)
        apply_texture.operator("uvmapping.bake_diffuse", icon="MATERIAL_DATA")

        status = layout.box()
        status.label(text=settings.texture_status, icon="INFO")
        if settings.texture_output_path:
            status.prop(settings, "texture_output_path", text="3면도")
        if settings.texture_diffuse_path:
            status.prop(settings, "texture_diffuse_path", text="Diffuse")

        advanced_toggle = layout.row(align=True)
        advanced_toggle.prop(
            settings,
            "show_texture_advanced",
            text="고급 설정",
            emboss=False,
            icon="TRIA_DOWN" if settings.show_texture_advanced else "TRIA_RIGHT",
        )
        if settings.show_texture_advanced:
            advanced = layout.box()
            if settings.texture_image_provider == "OPENAI":
                advanced.prop(settings, "texture_openai_analysis_model")
                advanced.prop(settings, "texture_openai_image_model")
            else:
                advanced.prop(settings, "texture_analysis_model")
                advanced.prop(settings, "texture_image_model")
            advanced.label(text="API 키는 Blender 환경설정에서 관리합니다", icon="KEYINGSET")


classes = (
    UVMAPPING_UL_reference_images,
    UVMAPPING_PT_ai_texture,
)


__all__ = (
    "classes",
    "UVMAPPING_PT_ai_texture",
    "UVMAPPING_UL_reference_images",
)
