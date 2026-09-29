"""AI 텍스처링 Blender 애드온 등록 모듈.

텍스처 계약·품질 코어는 Blender 밖에서도 테스트할 수 있도록 ``bpy``를 등록
시점에만 가져옵니다.
"""


_registered_classes = ()


def register():
    """애드온 클래스와 씬 설정을 등록합니다."""

    global _registered_classes

    import bpy
    from bpy.props import PointerProperty

    from . import texture_operators, ui
    from .properties import (
        UVMAPPING_AP_preferences,
        UVMAPPING_PG_reference_image,
        UVMAPPING_PG_settings,
        UVMAPPING_PG_target_object,
    )

    if _registered_classes:
        return

    classes = (
        UVMAPPING_AP_preferences,
        UVMAPPING_PG_reference_image,
        UVMAPPING_PG_target_object,
        UVMAPPING_PG_settings,
        *texture_operators.classes,
        *ui.classes,
    )
    registered = []
    scene_property_registered = False
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.Scene.uvmapping_settings = PointerProperty(type=UVMAPPING_PG_settings)
        scene_property_registered = True
    except Exception:
        if scene_property_registered and hasattr(bpy.types.Scene, "uvmapping_settings"):
            del bpy.types.Scene.uvmapping_settings
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
        _registered_classes = ()
        raise
    _registered_classes = tuple(registered)
    _schedule_api_key_restore()


def _schedule_api_key_restore():
    """등록 직후와 다음 이벤트 루프에서 OpenRouter API 키 백업 복구를 시도합니다.

    Blender 5.2는 register() 전에 환경설정 항목을 만들어 두므로 보통 즉시 복구된다.
    항목이 없는 드문 활성화 경로에 대비해 다음 이벤트 루프에서 한 번 더 시도한다.
    """

    import bpy

    from .properties import restore_api_key

    def attempt():
        try:
            restore_api_key()
        except Exception as error:  # 키 복구 실패가 애드온 등록을 막으면 안 된다.
            print(f"[UV Mapping] OpenRouter API 키 복구 실패: {error}")
        return None

    if _preferences_ready():
        attempt()
    else:
        bpy.app.timers.register(attempt, first_interval=0.0)


def _preferences_ready():
    """현재 애드온의 환경설정 인스턴스에 접근할 수 있는지 확인합니다."""

    import bpy

    from .properties import get_addon_preferences

    try:
        return get_addon_preferences(bpy.context) is not None
    except Exception:
        return False


def unregister():
    """씬 설정과 애드온 클래스를 역순으로 해제합니다."""

    global _registered_classes

    import bpy

    from . import texture_operators

    texture_operators.shutdown()

    if hasattr(bpy.types.Scene, "uvmapping_settings"):
        del bpy.types.Scene.uvmapping_settings
    for cls in reversed(_registered_classes):
        bpy.utils.unregister_class(cls)
    _registered_classes = ()


__all__ = ("register", "unregister")
