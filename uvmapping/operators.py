"""Blender 메시 분석과 자동 UV 언랩 연산자."""

from dataclasses import replace

import bmesh
import bpy
from bpy.types import Operator

from .analysis import analyze_mesh
from .types import AnalysisOptions


def _active_mesh_object(context):
    obj = context.view_layer.objects.active
    if obj is None or obj.type != "MESH" or obj.data is None:
        return None
    return obj


def _build_analysis_options(settings):
    options = AnalysisOptions.for_preset(settings.preset)
    overrides = {
        "preserve_existing_seams": settings.seam_policy == "PRESERVE",
        "ensure_cut_paths": settings.ensure_cut_paths,
        "connect_boundary_loops": settings.connect_boundary_loops,
    }
    if settings.use_custom_analysis:
        overrides.update(
            angle_weight=settings.angle_weight,
            concave_multiplier=settings.concave_multiplier,
            sharp_weight=settings.sharp_weight,
            material_weight=settings.material_weight,
            seam_threshold=settings.seam_threshold,
            min_chart_faces=settings.min_chart_faces,
        )
    return replace(options, **overrides)


def _capture_seams(mesh):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        return tuple(edge.seam for edge in bm.edges)
    finally:
        bm.free()


def _write_seams(mesh, seam_edges, preserve):
    seam_indices = set(seam_edges)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        for edge in bm.edges:
            edge.seam = edge.index in seam_indices or (preserve and edge.seam)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()


def _restore_seams(mesh, seam_snapshot):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        for edge, use_seam in zip(bm.edges, seam_snapshot, strict=False):
            edge.seam = use_seam
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()


def _capture_uv_layers(mesh):
    layers = []
    for layer in mesh.uv_layers:
        layers.append(
            {
                "name": layer.name,
                "active_render": layer.active_render,
                "active_clone": layer.active_clone,
                "loops": tuple(
                    ((float(loop.uv.x), float(loop.uv.y)), bool(loop.pin_uv))
                    for loop in layer.data
                ),
            }
        )
    return {
        "active_index": mesh.uv_layers.active_index if mesh.uv_layers else -1,
        "layers": layers,
    }


def _restore_uv_layers(mesh, snapshot):
    while mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[0])

    for layer_state in snapshot["layers"]:
        layer = mesh.uv_layers.new(name=layer_state["name"], do_init=False)
        for loop, (uv, pin_uv) in zip(layer.data, layer_state["loops"], strict=False):
            loop.uv = uv
            loop.pin_uv = pin_uv
        layer.active_render = layer_state["active_render"]
        layer.active_clone = layer_state["active_clone"]

    active_index = snapshot["active_index"]
    if 0 <= active_index < len(mesh.uv_layers):
        mesh.uv_layers.active_index = active_index
    mesh.update()


def _select_uv_layer(mesh, settings):
    name = settings.uv_layer_name.strip() or "AutoUV"
    if settings.create_new_uv_layer:
        layer = mesh.uv_layers.new(name=name, do_init=False)
    else:
        layer = mesh.uv_layers.get(name) or mesh.uv_layers.active
        if layer is None:
            layer = mesh.uv_layers.new(name=name, do_init=False)
    mesh.uv_layers.active_index = tuple(mesh.uv_layers).index(layer)
    return layer


def _unwrap_method():
    method_property = bpy.ops.uv.unwrap.get_rna_type().properties["method"]
    methods = {item.identifier for item in method_property.enum_items}
    return "MINIMUM_STRETCH" if "MINIMUM_STRETCH" in methods else "ANGLE_BASED"


def _require_finished(result, label):
    if "FINISHED" not in result:
        raise RuntimeError(f"{label} 연산이 완료되지 않았습니다: {sorted(result)}")


def _run_uv_pipeline(settings):
    method = _unwrap_method()
    unwrap_args = {
        "method": method,
        "fill_holes": settings.fill_holes,
        "correct_aspect": settings.correct_aspect,
        "margin": 0.0,
    }
    if method == "MINIMUM_STRETCH":
        unwrap_args["iterations"] = settings.unwrap_iterations

    result = bpy.ops.uv.unwrap(**unwrap_args)
    if "FINISHED" not in result and method == "MINIMUM_STRETCH":
        unwrap_args.pop("iterations", None)
        unwrap_args["method"] = "ANGLE_BASED"
        result = bpy.ops.uv.unwrap(**unwrap_args)
    _require_finished(result, "UV 언랩")
    _require_finished(
        bpy.ops.uv.average_islands_scale(scale_uv=True, shear=True),
        "아일랜드 스케일 정규화",
    )
    _require_finished(
        bpy.ops.uv.pack_islands(
            rotate=True,
            scale=True,
            margin_method="SCALED",
            margin=settings.island_margin,
        ),
        "UV 패킹",
    )
    return method


class _ContextState:
    """연산 중 임시로 바꾸는 모드와 선택 상태를 복구합니다."""

    def __init__(self, context, target):
        self.context = context
        self.target = target
        self.active_object = context.view_layer.objects.active
        self.target_mode = target.mode
        self.object_selection = tuple(
            (obj, obj.select_get()) for obj in context.view_layer.objects
        )
        self.mesh_select_mode = tuple(context.tool_settings.mesh_select_mode)
        self.use_uv_select_sync = context.tool_settings.use_uv_select_sync
        self.mesh_selection = None
        self.prepared = False

    def prepare(self):
        # 이 시점부터 모드를 바꾸므로 중간 예외가 나도 원래 상태를 복구합니다.
        self.prepared = True
        if self.target.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        mesh = self.target.data
        self.mesh_selection = (
            tuple(vertex.select for vertex in mesh.vertices),
            tuple(edge.select for edge in mesh.edges),
            tuple(polygon.select for polygon in mesh.polygons),
        )

        for obj, is_selected in self.object_selection:
            if is_selected:
                obj.select_set(False)
        self.target.select_set(True)
        self.context.view_layer.objects.active = self.target

    def restore(self):
        if not self.prepared:
            return

        current = self.context.view_layer.objects.active
        if current is not None and current.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        if self.mesh_selection is not None:
            vertices, edges, polygons = self.mesh_selection
            for item, is_selected in zip(self.target.data.vertices, vertices, strict=False):
                item.select = is_selected
            for item, is_selected in zip(self.target.data.edges, edges, strict=False):
                item.select = is_selected
            for item, is_selected in zip(self.target.data.polygons, polygons, strict=False):
                item.select = is_selected
            self.target.data.update()

        for obj, is_selected in self.object_selection:
            try:
                obj.select_set(is_selected)
            except RuntimeError:
                pass
        self.context.view_layer.objects.active = self.active_object
        self.context.tool_settings.mesh_select_mode = self.mesh_select_mode
        self.context.tool_settings.use_uv_select_sync = self.use_uv_select_sync

        if self.target_mode != "OBJECT" and self.active_object is not None:
            bpy.ops.object.mode_set(mode=self.target_mode)


class UVMAPPING_OT_analyze(Operator):
    """활성 메시를 변경하지 않고 Seam 후보를 분석합니다."""

    bl_idname = "uvmapping.analyze"
    bl_label = "Seam 분석"
    bl_description = "활성 메시를 분석하고 예상 Seam 수를 표시합니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = _active_mesh_object(context)
        return obj is not None and len(obj.data.polygons) > 0

    def execute(self, context):
        obj = _active_mesh_object(context)
        settings = context.scene.uvmapping_settings
        state = _ContextState(context, obj)
        try:
            state.prepare()
            result = analyze_mesh(obj.data, _build_analysis_options(settings))
            chart_count = getattr(result, "chart_count", 0)
            message = f"Seam {len(result.seam_edges)}개, 예상 차트 {chart_count}개"
            settings.last_result = message
            warnings = getattr(result, "warnings", ())
            if warnings:
                self.report({"WARNING"}, f"{message} · {warnings[0]}")
            else:
                self.report({"INFO"}, message)
            return {"FINISHED"}
        except Exception as exc:
            settings.last_result = f"분석 실패: {exc}"
            self.report({"ERROR"}, settings.last_result)
            return {"CANCELLED"}
        finally:
            state.restore()


class UVMAPPING_OT_auto_unwrap(Operator):
    """Seam 분석부터 UV 패킹까지 한 번에 수행합니다."""

    bl_idname = "uvmapping.auto_unwrap"
    bl_label = "자동 UV 언랩"
    bl_description = "메시를 분석해 Seam을 적용하고 UV 언랩과 패킹을 수행합니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = _active_mesh_object(context)
        return obj is not None and len(obj.data.polygons) > 0

    def execute(self, context):
        obj = _active_mesh_object(context)
        mesh = obj.data
        settings = context.scene.uvmapping_settings
        state = _ContextState(context, obj)
        seam_snapshot = None
        uv_snapshot = None

        try:
            state.prepare()
            seam_snapshot = _capture_seams(mesh)
            uv_snapshot = _capture_uv_layers(mesh)
            analysis = analyze_mesh(mesh, _build_analysis_options(settings))
            _write_seams(
                mesh,
                analysis.seam_edges,
                preserve=settings.seam_policy == "PRESERVE",
            )
            layer = _select_uv_layer(mesh, settings)
            layer_name = layer.name

            bpy.ops.object.mode_set(mode="EDIT")
            context.tool_settings.use_uv_select_sync = True
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.select_all(action="SELECT")
            method = _run_uv_pipeline(settings)
            bpy.ops.object.mode_set(mode="OBJECT")

            chart_count = getattr(analysis, "chart_count", 0)
            message = (
                f"{layer_name}: Seam {len(analysis.seam_edges)}개, "
                f"차트 {chart_count}개, {method}"
            )
            settings.last_result = message
            warnings = getattr(analysis, "warnings", ())
            if warnings:
                self.report({"WARNING"}, f"{message} · {warnings[0]}")
            else:
                self.report({"INFO"}, message)
            return {"FINISHED"}
        except Exception as exc:
            try:
                if obj.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
                if seam_snapshot is not None:
                    _restore_seams(mesh, seam_snapshot)
                if uv_snapshot is not None:
                    _restore_uv_layers(mesh, uv_snapshot)
            except Exception as rollback_exc:
                self.report({"ERROR"}, f"롤백 실패: {rollback_exc}")
            settings.last_result = f"자동 언랩 실패: {exc}"
            self.report({"ERROR"}, settings.last_result)
            return {"CANCELLED"}
        finally:
            state.restore()


classes = (
    UVMAPPING_OT_analyze,
    UVMAPPING_OT_auto_unwrap,
)


__all__ = ("classes", "UVMAPPING_OT_analyze", "UVMAPPING_OT_auto_unwrap")
