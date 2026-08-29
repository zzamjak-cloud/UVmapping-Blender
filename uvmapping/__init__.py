"""UV Mapping Blender 등록 모듈.

코어 분석기는 Blender 밖에서도 테스트할 수 있도록 ``bpy``를 등록 시점에만
가져옵니다.
"""


_registered_classes = ()


def register():
    """애드온 클래스와 씬 설정을 등록합니다."""

    global _registered_classes

    import bpy
    from bpy.props import PointerProperty

    from . import operators, ui
    from .properties import UVMAPPING_PG_settings

    classes = (
        UVMAPPING_PG_settings,
        *operators.classes,
        *ui.classes,
    )
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.uvmapping_settings = PointerProperty(type=UVMAPPING_PG_settings)
    _registered_classes = classes


def unregister():
    """씬 설정과 애드온 클래스를 역순으로 해제합니다."""

    global _registered_classes

    import bpy

    if hasattr(bpy.types.Scene, "uvmapping_settings"):
        del bpy.types.Scene.uvmapping_settings
    for cls in reversed(_registered_classes):
        bpy.utils.unregister_class(cls)
    _registered_classes = ()


__all__ = ("register", "unregister")
