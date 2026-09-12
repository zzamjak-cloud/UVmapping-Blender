"""UV Mapping Blender Extension 진입점."""

from .uvmapping import register, unregister


bl_info = {
    "name": "UV Mapping Blender",
    "author": "zzamjak-cloud",
    "version": (2, 3, 0),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar > UV Mapping",
    "description": "참조 이미지와 프롬프트로 AI 다면도를 생성하고 기존 UV에 손맵 텍스처를 베이크합니다.",
    "category": "Material",
}


__all__ = ("register", "unregister")
