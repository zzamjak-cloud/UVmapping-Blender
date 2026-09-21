"""3D View 사이드바 사용자 인터페이스."""

import os
from pathlib import Path

import bpy
from bpy.types import Panel, UIList

from .openrouter_provider import clamp_quality, supports_resolution
from .properties import get_addon_preferences, quality_values_from
from . import texture_errors
from .texture_operators import (
    can_bake_diffuse,
    can_generate_turnaround,
    has_applied_diffuse,
    has_stalled_texture_state,
    registered_targets,
    texture_targets,
    texture_verification_summary,
)
from .texture_pipeline import resolve_composition
from .texture_presets import (
    matching_quality_preset,
    resolved_image_quality,
    turnaround_call_count,
)


def _has_texture_target(context) -> bool:
    """등록 대상이나 선택 Mesh가 있어야 텍스처 단계를 노출한다."""

    return bool(texture_targets(context))


def _missing_uv_names(context) -> tuple[str, ...]:
    """활성 UV 맵이 없는 대상 객체 이름을 모은다."""

    return tuple(
        obj.name for obj in texture_targets(context) if obj.data.uv_layers.active is None
    )


def _uses_quality_steps(settings) -> bool:
    """이미지 모델이 해상도 대신 품질 단계를 받는지."""

    return not supports_resolution(settings.texture_image_model)


def _quality_grades(settings) -> tuple[str, str]:
    """(고른 등급, 실제로 보낼 등급).

    Provider가 모델 상한을 넘는 등급을 낮춰 보내므로, 패널에도 낮아진 값을 보여야
    사용자가 고른 값과 결과가 어긋나 보이지 않는다.
    """

    resolved = resolved_image_quality(
        settings.texture_quality_preset, settings.texture_image_quality
    )
    return resolved, clamp_quality(settings.texture_image_model, resolved)


def _draw_quality_steps(layout, settings) -> None:
    """1K 고정 안내와 실제 전송 등급. 상한으로 낮아졌으면 그 사실까지 적는다."""

    resolved, sent = _quality_grades(settings)
    layout.label(text=f"1K 고정 · 품질 단계 {sent}", icon="INFO")
    if sent != resolved:
        layout.label(text=f"{resolved}는 이 모델의 상한을 넘어 {sent}로 낮춰 보냅니다", icon="INFO")


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

        self._draw_problems(layout, context)
        self._draw_targets(layout, context, settings)
        self._draw_references(layout, settings)
        self._draw_quality(layout, settings)
        self._draw_actions(layout, context, settings)
        self._draw_status(layout, context, settings)
        self._draw_advanced(layout, settings)

    @staticmethod
    def _draw_problems(layout, context) -> None:
        """진행을 막는 조건만 보여 준다. 정상 상태에서는 아무것도 그리지 않는다."""

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

        preferences = get_addon_preferences(context)
        api_key = getattr(preferences, "openrouter_api_key", "") or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )
        if not api_key.strip():
            key_box = layout.box()
            key_box.label(text="OpenRouter API 키가 필요합니다", icon="ERROR")
            key_box.label(text="환경설정 > 애드온에서 키 하나만 입력하면 됩니다")
            key_box.operator(
                "screen.userpref_show", text="환경설정 열기", icon="PREFERENCES"
            )

        missing_uv = _missing_uv_names(context)
        if missing_uv:
            uv_box = layout.box()
            uv_box.label(text="UV 맵이 없는 객체가 있습니다", icon="ERROR")
            uv_box.label(text=", ".join(missing_uv))
            uv_box.label(text="UV Editing에서 UV를 펼친 뒤 0-1 안에 배치해 주세요")

    @staticmethod
    def _draw_targets(layout, context, settings) -> None:
        target_box = layout.box()
        target_box.label(text="대상 객체", icon="OUTLINER_OB_MESH")
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
            controls = target_box.row(align=True)
            controls.operator(
                "uvmapping.add_target_objects", text="선택 객체 등록", icon="ADD"
            )
            controls.operator("uvmapping.remove_target_object", text="", icon="REMOVE")
            controls.operator("uvmapping.clear_target_objects", text="", icon="X")
            target_box.label(
                text=f"등록 {len(registered_targets(context))}개를 대상으로 진행합니다",
                icon="CHECKMARK",
            )
            return
        target_box.label(
            text=f"선택한 Mesh {len(texture_targets(context))}개를 대상으로 합니다",
            icon="INFO",
        )
        target_box.operator(
            "uvmapping.add_target_objects", text="선택 객체 등록", icon="ADD"
        )

    @staticmethod
    def _draw_references(layout, settings) -> None:
        reference_box = layout.box()
        reference_box.label(text="스타일 참조와 지시", icon="IMAGE_DATA")
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
        if not settings.reference_images:
            reference_box.label(
                text="참조 없이도 아래 지시만으로 생성할 수 있습니다", icon="INFO"
            )
        reference_box.prop(settings, "texture_user_prompt", text="")
        reference_box.operator(
            "uvmapping.edit_texture_prompt", text="한글 프롬프트 입력", icon="TEXT"
        )

    @staticmethod
    def _draw_quality(layout, settings) -> None:
        quality_box = layout.box()
        quality_box.label(text="품질과 모델", icon="PRESET")
        quality_box.prop(settings, "texture_quality_preset", expand=True)
        quality_box.prop(settings, "texture_model_preset", text="")
        call_count = turnaround_call_count(
            settings.turnaround_layout, settings.generation_mode
        )
        if call_count > 1:
            quality_box.label(
                text=f"OpenRouter 호출 {call_count}회 · 비용 약 {call_count}배",
                icon="ERROR",
            )
        else:
            quality_box.label(text="OpenRouter 호출 1회", icon="INFO")
        if _uses_quality_steps(settings):
            _draw_quality_steps(quality_box, settings)
        # [기존 씬 호환] 저장된 세부 값이 프리셋 표와 다를 수 있다. 값을 덮어쓰지 않고
        # 표시로만 알린다.
        if matching_quality_preset(quality_values_from(settings)) != settings.texture_quality_preset:
            quality_box.label(
                text="저장된 세부 값이 이 프리셋과 달라 실제로는 사용자 지정 상태입니다",
                icon="INFO",
            )

    @staticmethod
    def _draw_actions(layout, context, settings) -> None:
        generate = layout.column()
        generate.scale_y = 1.5
        generate.enabled = can_generate_turnaround(context)
        generate.operator(
            "uvmapping.generate_turnaround", text="텍스처 생성", icon="RENDER_STILL"
        )
        applied = has_applied_diffuse(context)
        if can_bake_diffuse(context):
            # 적용을 끝낸 뒤에도 베이크는 무과금이라 다시 적용할 길을 남긴다.
            apply_texture = layout.column()
            apply_texture.scale_y = 1.5
            apply_texture.operator(
                "uvmapping.bake_diffuse",
                text="Diffuse 다시 적용" if applied else "Diffuse 적용",
                icon="MATERIAL_DATA",
            )
        if settings.auto_apply_diffuse and not applied:
            layout.label(text="생성이 끝나면 자동으로 적용합니다", icon="CHECKMARK")
        if has_stalled_texture_state(context):
            # 중단된 순차 생성과 그룹 실패 상태는 베이크할 수 없으므로 초기화 길을 바로 보여 준다.
            stalled = layout.row()
            stalled.alert = True
            stalled.operator("uvmapping.reset_texture_state", icon="TRASH")

    def _draw_status(self, layout, context, settings) -> None:
        summary = texture_verification_summary(context)
        if summary is not None:
            self._draw_verification(layout, summary)

        status = layout.box()
        status.label(text=settings.texture_status, icon="INFO")
        if settings.texture_last_error:
            # 상태 줄 한 줄에는 원인이 잘려 들어가므로 분류 제목과 팝업 통로를 함께 둔다.
            failure = status.column(align=True)
            failure.alert = True
            failure.label(
                text=texture_errors.describe(settings.texture_last_error_kind).title,
                icon="ERROR",
            )
            failure.operator(
                "uvmapping.show_texture_failure", text="실패 원인 보기", icon="QUESTION"
            )
        if settings.texture_output_path:
            status.prop(settings, "texture_output_path", text="다면도")
        if settings.texture_diffuse_path:
            status.prop(settings, "texture_diffuse_path", text="Diffuse")

    @staticmethod
    def _draw_advanced(layout, settings) -> None:
        advanced_toggle = layout.row(align=True)
        advanced_toggle.prop(
            settings,
            "show_texture_advanced",
            text="고급 설정",
            emboss=False,
            icon="TRIA_DOWN" if settings.show_texture_advanced else "TRIA_RIGHT",
        )
        if not settings.show_texture_advanced:
            return

        advanced = layout.box()
        advanced.prop(settings, "turnaround_layout", text="")
        quality_steps = _uses_quality_steps(settings)
        if quality_steps:
            # 크기를 받지 않는 모델에서 "생성 이미지 크기"는 아무 효과가 없어 혼란만 준다.
            advanced.prop(settings, "texture_image_quality")
            _draw_quality_steps(advanced, settings)
        else:
            advanced.prop(settings, "turnaround_image_size")
        advanced.prop(settings, "generation_mode", text="")
        composition = resolve_composition(settings.turnaround_layout)
        if quality_steps:
            advanced.label(
                text="시트 한 장이 1K라 6면도(3×2)는 시점당 픽셀이 낮고 4회 호출이 유리합니다",
                icon="INFO",
            )
        sequential = settings.generation_mode == "SEQUENTIAL"
        if not sequential:
            if len(composition.rounds) > 1:
                advanced.label(
                    text="정면을 먼저 만든 뒤 나머지를 그 색에 맞춰 병렬 생성합니다(2라운드)",
                    icon="INFO",
                )
            advanced.prop(settings, "auto_regenerate_attempts")
            if settings.auto_regenerate_attempts:
                advanced.label(
                    text=(
                        f"불일치 시 최대 {settings.auto_regenerate_attempts}회 재시도"
                        f"(회당 최대 {len(composition.groups)}회 호출)"
                    ),
                    icon="INFO",
                )
        if "TOP" in composition.views:
            advanced.label(
                text="상·하·좌 시점을 실제 그림으로 받아 투영합니다", icon="CHECKMARK"
            )
        else:
            advanced.label(text="상·하면은 측면 색을 늘려 채웁니다", icon="INFO")

        advanced.prop(settings, "texture_resolution")
        advanced.prop(settings, "padding_pixels")
        advanced.prop(settings, "auto_apply_diffuse")
        advanced.prop(settings, "verify_after_bake")

        if settings.reference_images:
            advanced.prop(settings, "send_reference_images")
            if settings.send_reference_images:
                advanced.label(
                    text="참조 형상이 복제되어 모델 실루엣을 무시할 수 있습니다", icon="ERROR"
                )
            analyze = advanced.row()
            analyze.operator("uvmapping.analyze_references", icon="VIEWZOOM")

        expert_toggle = advanced.row(align=True)
        expert_toggle.prop(
            settings,
            "show_texture_expert",
            text="전문가 설정",
            emboss=False,
            icon="TRIA_DOWN" if settings.show_texture_expert else "TRIA_RIGHT",
        )
        if not settings.show_texture_expert:
            return
        expert = advanced.box()
        expert.prop(settings, "blend_exponent")
        expert.prop(settings, "dominant_view_blend")
        band = expert.row()
        # 지배 시점을 쓰지 않으면 전이 띠 폭은 합성에 영향을 주지 않는다.
        band.enabled = settings.dominant_view_blend
        band.prop(settings, "transition_band_degrees")
        expert.prop(settings, "harmonize_view_colors")
        expert.prop(settings, "silhouette_warp")
        expert.prop(settings, "texture_analysis_model")
        expert.prop(settings, "texture_image_model")
        expert.label(
            text="openrouter.ai/models의 모델 식별자를 직접 넣을 수 있습니다", icon="INFO"
        )

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
