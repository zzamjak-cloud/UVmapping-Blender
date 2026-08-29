"""UV Mapping Blender Extension 진입점."""

from .uvmapping import register, unregister


bl_info = {
    "name": "UV Mapping Blender",
    "author": "zzamjak-cloud",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar > UV Mapping",
    "description": "메시를 분석해 Seam 생성, UV 언랩, 스케일 정규화와 패킹을 자동화합니다.",
    "category": "UV",
}


__all__ = ("register", "unregister")
