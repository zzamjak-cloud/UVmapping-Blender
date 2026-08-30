"""3D View 사이드바 사용자 인터페이스."""

import bpy
from bpy.types import Panel

from . import preview


class UVMAPPING_PT_main(Panel):
    """UV 언랩 기본 패널."""

    bl_label = "UV 언랩"
    bl_idname = "UVMAPPING_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UV Mapping"

    @classmethod
    def poll(cls, context):
        candidates = getattr(context, "selected_editable_objects", ())
        return preview.is_active() or any(
            obj.type == "MESH"
            and obj.data is not None
            and len(obj.data.polygons) > 0
            for obj in candidates
        )

    def draw(self, context):
        layout = self.layout
        settings = context.scene.uvmapping_settings

        layout.prop(settings, "preset", text="")
        layout.prop(settings, "quality_level", text="품질")
        button = layout.column()
        button.scale_y = 1.6
        button.operator("uvmapping.auto_unwrap", icon="UV")

        preview = layout.row(align=True)
        preview.operator("uvmapping.preview_seams", icon="SHADING_WIRE")
        preview.operator("uvmapping.clear_preview", icon="X")

        if settings.last_result:
            status = layout.box()
            status.label(text=settings.last_result, icon="INFO")

        packing = layout.box()
        packing.label(text="출력 텍스처", icon="TEXTURE")
        packing.prop(settings, "texture_resolution")
        packing.prop(settings, "padding_pixels")
        packing.prop(settings, "pack_shared_atlas")

        advanced = layout.row(align=True)
        advanced.prop(
            settings,
            "show_advanced",
            text="고급 설정",
            emboss=False,
            icon="TRIA_DOWN" if settings.show_advanced else "TRIA_RIGHT",
        )
        if not settings.show_advanced:
            return

        column = layout.column(align=True)
        column.prop(settings, "seam_policy")
        column.prop(settings, "create_new_uv_layer")
        column.prop(settings, "uv_layer_name")
        column.prop(settings, "generate_texture_job")
        column.separator()
        column.prop(settings, "unwrap_iterations")
        column.prop(settings, "fill_holes")
        column.prop(settings, "correct_aspect")

        analysis = layout.box()
        analysis.prop(settings, "use_custom_analysis")
        if settings.use_custom_analysis:
            grid = analysis.grid_flow(columns=2, even_columns=True, align=True)
            grid.prop(settings, "angle_weight")
            grid.prop(settings, "concave_multiplier")
            grid.prop(settings, "sharp_weight")
            grid.prop(settings, "material_weight")
            grid.prop(settings, "seam_threshold")
            grid.prop(settings, "min_chart_faces")
        analysis.prop(settings, "ensure_cut_paths")
        analysis.prop(settings, "connect_boundary_loops")
        analysis.operator("uvmapping.analyze", icon="VIEWZOOM")


classes = (UVMAPPING_PT_main,)


__all__ = ("classes", "UVMAPPING_PT_main")
