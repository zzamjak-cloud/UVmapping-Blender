"""3D View 사이드바 사용자 인터페이스."""

import os
from pathlib import Path

import bpy
from bpy.types import Panel, UIList

from .properties import get_addon_preferences
from .texture_operators import (
    can_bake_diffuse,
    has_stalled_sequential_state,
    registered_targets,
    texture_targets,
    texture_verification_summary,
)
from .texture_pipeline import resolve_layout


def _has_texture_target(context) -> bool:
    """등록 대상이나 선택 Mesh가 있어야 텍스처 단계를 노출한다."""

    return bool(texture_targets(context))


def _missing_uv_names(context) -> tuple[str, ...]:
    """활성 UV 맵이 없는 대상 객체 이름을 모은다."""

    return tuple(
        obj.name for obj in texture_targets(context) if obj.data.uv_layers.active is None
    )


class UVMAPPING_UL_target_objects(UIList):
    """3면도와 텍스처의 대상으로 등록한 Mesh 객체 목록."""

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
        obj = item.object
        if obj is None:
            layout.label(text="(삭제된 객체)", icon="ERROR")
            return
        row = layout.row(align=True)
        row.label(text=obj.name, icon="OUTLINER_OB_MESH")
        if obj.data.uv_layers.active is None:
            row.label(text="", icon="ERROR")


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
    """스타일 참조 분석과 한 장짜리 모델 다면도 생성 패널."""

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

        target_box = layout.box()
        target_box.label(text="대상 객체", icon="OUTLINER_OB_MESH")
        registered = registered_targets(context)
        if settings.target_objects:
            target_box.template_list(
                "UVMAPPING_UL_target_objects",
                "",
                settings,
                "target_objects",
                settings,
                "target_object_index",
                rows=3,
            )
        else:
            target_box.label(
                text="등록이 없으면 현재 선택한 Mesh를 대상으로 합니다", icon="INFO"
            )
        target_controls = target_box.row(align=True)
        target_controls.operator(
            "uvmapping.add_target_objects", text="선택 객체 등록", icon="ADD"
        )
        target_controls.operator(
            "uvmapping.remove_target_object", text="", icon="REMOVE"
        )
        target_controls.operator("uvmapping.clear_target_objects", text="", icon="X")
        if registered:
            target_box.label(
                text=f"등록 {len(registered)}개를 대상으로 진행합니다", icon="CHECKMARK"
            )

        missing_uv = _missing_uv_names(context)
        if missing_uv:
            uv_box = layout.box()
            uv_box.label(text="UV 맵이 없는 객체가 있습니다", icon="ERROR")
            uv_box.label(text=", ".join(missing_uv))
            uv_box.label(text="UV Editing에서 UV를 펼친 뒤 0-1 안에 배치해 주세요")

        key_box = layout.box()
        key_box.label(text="OpenRouter", icon="KEYINGSET")
        preferences = get_addon_preferences(context)
        api_key = getattr(preferences, "openrouter_api_key", "") or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )
        if api_key.strip():
            key_box.label(text="OpenRouter API 키 사용 가능", icon="CHECKMARK")
        else:
            key_box.label(text="OpenRouter API 키가 필요합니다", icon="ERROR")
            key_box.label(text="환경설정 > 애드온에서 키 하나만 입력하면 됩니다")
            key_box.operator(
                "screen.userpref_show", text="환경설정 열기", icon="PREFERENCES"
            )

        model_box = layout.box()
        model_box.label(text="이미지 생성 모델", icon="IMAGE_DATA")
        model_choices = model_box.column(align=True)
        model_choices.prop(settings, "texture_model_preset", expand=True)
        model_box.label(text=settings.texture_image_model, icon="DOT")

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
        has_references = bool(settings.reference_images)
        analyze = reference_box.row()
        analyze.enabled = has_references
        analyze.operator("uvmapping.analyze_references", icon="VIEWZOOM")
        if not has_references:
            reference_box.label(
                text="참조 없이도 아래 프롬프트만으로 생성할 수 있습니다", icon="INFO"
            )
        else:
            reference_box.prop(settings, "send_reference_images")
            if settings.send_reference_images:
                reference_box.label(
                    text="참조 형상이 복제되어 모델 실루엣을 무시할 수 있습니다", icon="ERROR"
                )
            else:
                reference_box.label(
                    text="스타일은 분석 결과로만 전달해 모델 형상을 지킵니다", icon="CHECKMARK"
                )

        prompt_box = layout.box()
        prompt_box.label(text="추가 지시", icon="TEXT")
        prompt_box.prop(settings, "texture_user_prompt", text="")
        prompt_box.operator(
            "uvmapping.edit_texture_prompt", text="한글 프롬프트 입력", icon="TEXT"
        )

        composition_box = layout.box()
        composition_box.label(text="다면도 구성", icon="MESH_GRID")
        composition_box.prop(settings, "turnaround_layout", text="")
        composition_box.prop(settings, "turnaround_image_size")
        composition_box.prop(settings, "generation_mode", text="")
        if settings.generation_mode == "SEQUENTIAL":
            call_count = len(resolve_layout(settings.turnaround_layout).views)
            composition_box.label(
                text=f"OpenRouter 호출 {call_count}회 · 비용 {call_count}배", icon="ERROR"
            )
        else:
            composition_box.prop(settings, "auto_regenerate_attempts")
            if settings.auto_regenerate_attempts:
                composition_box.label(
                    text=f"불일치 시 최대 {settings.auto_regenerate_attempts}회 추가 호출", icon="INFO"
                )
        if settings.turnaround_layout == "SIX":
            composition_box.label(
                text="상·하·좌 시점을 실제 그림으로 받아 투영합니다", icon="CHECKMARK"
            )
        else:
            composition_box.label(
                text="상·하면은 측면 색을 늘려 채웁니다", icon="INFO"
            )

        output_box = layout.box()
        output_box.label(text="출력 텍스처", icon="TEXTURE")
        output_box.prop(settings, "texture_resolution")
        output_box.prop(settings, "padding_pixels")
        output_box.prop(settings, "auto_apply_diffuse")
        blend_column = output_box.column(align=True)
        blend_column.label(text="투영 블렌딩", icon="NODE_MATERIAL")
        blend_column.prop(settings, "blend_exponent")
        blend_column.prop(settings, "harmonize_view_colors")
        blend_column.prop(settings, "silhouette_warp")
        output_box.prop(settings, "verify_after_bake")

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
        if has_stalled_sequential_state(context):
            # 중단된 순차 생성은 베이크할 수 없으므로 상태를 지우는 길을 바로 보여 준다.
            stalled = layout.row()
            stalled.alert = True
            stalled.operator("uvmapping.reset_texture_state", icon="TRASH")
        if settings.auto_apply_diffuse:
            layout.label(text="생성이 끝나면 자동으로 적용합니다", icon="CHECKMARK")

        summary = texture_verification_summary(context)
        if summary is not None:
            self._draw_verification(layout, summary)

        status = layout.box()
        status.label(text=settings.texture_status, icon="INFO")
        if settings.texture_output_path:
            status.prop(settings, "texture_output_path", text="다면도")
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
            advanced.prop(settings, "texture_analysis_model")
            advanced.prop(settings, "texture_image_model")
            advanced.label(text="openrouter.ai/models의 모델 식별자를 직접 넣을 수 있습니다", icon="INFO")
            advanced.label(text="API 키는 Blender 환경설정에서 관리합니다", icon="KEYINGSET")

    @staticmethod
    def _draw_verification(layout, summary: dict) -> None:
        """사전 실루엣 검증과 적용 후 검증 결과. 재생성이 필요한지 한눈에 보이게 한다."""

        verification = summary.get("verification") or {}
        silhouette_failed = list(summary.get("silhouette_failed") or [])
        views = verification.get("views") or {}
        recommend = bool(silhouette_failed) or any(
            not item.get("passed", True) for item in views.values()
        ) or bool(verification.get("error"))
        box = layout.box()
        header = box.row()
        header.label(text="검증", icon="ERROR" if recommend else "CHECKMARK")
        if summary.get("attempt"):
            header.label(text=f"재생성 {summary['attempt']}회")
        if silhouette_failed:
            box.label(text=f"실루엣 불일치: {', '.join(silhouette_failed)}", icon="ERROR")
        elif summary.get("silhouette_error"):
            box.label(text="실루엣 검증 실패", icon="INFO")
        if views:
            grid = box.grid_flow(columns=3, align=True)
            for view, item in views.items():
                score = float(item.get("score", 0.0))
                cell = grid.row()
                cell.alert = not item.get("passed", True)
                cell.label(text=f"{view} {score:.2f}")
        if verification.get("error"):
            box.label(text=f"적용 후 검증 실패: {verification['error']}", icon="INFO")
        sheet = verification.get("sheet")
        if sheet:
            box.operator("wm.path_open", text="검증 시트 열기", icon="IMAGE_DATA").filepath = str(sheet)
        if recommend:
            box.label(text="재생성 권장", icon="ERROR")


classes = (
    UVMAPPING_UL_reference_images,
    UVMAPPING_UL_target_objects,
    UVMAPPING_PT_ai_texture,
)


__all__ = (
    "classes",
    "UVMAPPING_PT_ai_texture",
    "UVMAPPING_UL_reference_images",
    "UVMAPPING_UL_target_objects",
)
