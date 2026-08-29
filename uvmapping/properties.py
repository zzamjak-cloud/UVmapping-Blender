"""UV 자동 언랩 설정 속성."""

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import PropertyGroup


def _sync_island_margin(settings, _context) -> None:
    """사용자용 픽셀 여백을 기존 연산자가 읽는 UV fraction으로 변환한다."""

    resolution = max(1, int(settings.texture_resolution))
    settings.island_margin = settings.padding_pixels / resolution


class UVMAPPING_PG_settings(PropertyGroup):
    """씬에 저장되는 자동 언랩 설정."""

    preset: EnumProperty(
        name="프리셋",
        description="메시 유형에 맞는 Seam 분석 가중치를 선택합니다",
        items=(
            ("ORGANIC", "Organic", "부드러운 곡률과 오목한 영역을 우선합니다"),
            ("BALANCED", "Balanced", "유기체와 하드서페이스에 균형 잡힌 설정입니다"),
            ("HARD_SURFACE", "Hard Surface", "Sharp Edge와 재질 경계를 강하게 반영합니다"),
        ),
        default="BALANCED",
    )
    quality_level: EnumProperty(
        name="품질 단계",
        description="비교할 자동 Seam 후보 수를 선택합니다",
        items=(
            ("FAST", "빠르게", "기본 후보 한 개만 평가합니다"),
            ("BALANCED", "균형", "최대 세 후보를 비교합니다"),
            ("QUALITY", "품질 우선", "최대 다섯 후보를 비교합니다"),
        ),
        default="BALANCED",
    )
    process_selected_objects: BoolProperty(
        name="선택 객체 모두 처리",
        description="활성 객체만이 아니라 선택된 편집 가능 Mesh 객체를 모두 처리합니다",
        default=True,
    )
    generate_texture_job: BoolProperty(
        name="TextureJob 생성",
        description="AI 텍스처 단계가 사용할 품질과 UV 구조 계약을 객체 속성에 저장합니다",
        default=True,
    )
    seam_policy: EnumProperty(
        name="기존 Seam",
        description="기존 Seam을 자동 분석 결과와 합칠지 결정합니다",
        items=(
            ("PRESERVE", "보존", "기존 Seam을 유지하고 새 Seam을 추가합니다"),
            ("REPLACE", "교체", "기존 Seam을 지우고 분석 결과만 적용합니다"),
        ),
        default="PRESERVE",
    )
    create_new_uv_layer: BoolProperty(
        name="새 UV 레이어 생성",
        description="기존 UV를 덮어쓰지 않고 새 UV 레이어에 결과를 만듭니다",
        default=True,
    )
    uv_layer_name: StringProperty(
        name="UV 레이어 이름",
        description="새로 만들거나 사용할 UV 레이어 이름입니다",
        default="AutoUV",
    )
    unwrap_iterations: IntProperty(
        name="최소 스트레치 반복",
        description="Minimum Stretch 언랩의 최적화 반복 횟수입니다",
        default=10,
        min=1,
        max=10000,
    )
    texture_resolution: EnumProperty(
        name="텍스처 크기",
        description="UV를 배치할 정사각형 텍스처의 가로세로 픽셀 크기입니다",
        items=(
            ("1024", "1024 px", "1K 텍스처를 대상으로 합니다"),
            ("2048", "2048 px", "2K 텍스처를 대상으로 합니다"),
            ("4096", "4096 px", "4K 텍스처를 대상으로 합니다"),
            ("8192", "8192 px", "8K 텍스처를 대상으로 합니다"),
        ),
        default="2048",
        update=_sync_island_margin,
    )
    padding_pixels: IntProperty(
        name="UV 조각 여백",
        description="각 UV 조각 둘레에 확보할 픽셀 여백입니다. 두 조각 사이 간격은 이 값의 약 두 배입니다",
        default=16,
        min=0,
        max=256,
        subtype="PIXEL",
        update=_sync_island_margin,
    )
    pack_shared_atlas: BoolProperty(
        name="선택 객체를 한 장에 배치",
        description="선택한 모든 Mesh의 UV를 겹치지 않게 한 장의 텍스처 공간에 함께 배치합니다",
        default=True,
    )
    island_margin: FloatProperty(
        name="내부 UV 여백",
        description="픽셀 패딩에서 자동 환산되는 내부 호환용 UV fraction입니다",
        default=0.0078125,
        min=0.0,
        max=0.5,
        precision=4,
        subtype="FACTOR",
        options={"HIDDEN"},
    )
    fill_holes: BoolProperty(
        name="구멍 채움 고려",
        description="언랩 계산에서 내부 구멍을 채운 것으로 처리합니다",
        default=True,
    )
    correct_aspect: BoolProperty(
        name="이미지 비율 보정",
        description="활성 이미지의 가로세로 비율을 UV 계산에 반영합니다",
        default=True,
    )
    show_advanced: BoolProperty(
        name="고급 설정",
        default=False,
    )
    use_custom_analysis: BoolProperty(
        name="프리셋 세부 조정",
        description="프리셋의 일부 분석 값을 아래 값으로 덮어씁니다",
        default=False,
    )
    angle_weight: FloatProperty(
        name="각도 가중치",
        default=1.0,
        min=0.0,
        max=10.0,
    )
    concave_multiplier: FloatProperty(
        name="오목부 배수",
        default=1.35,
        min=0.0,
        max=10.0,
    )
    sharp_weight: FloatProperty(
        name="Sharp 가중치",
        default=1.5,
        min=0.0,
        max=10.0,
    )
    material_weight: FloatProperty(
        name="재질 경계 가중치",
        default=1.0,
        min=0.0,
        max=10.0,
    )
    seam_threshold: FloatProperty(
        name="Seam 임계값",
        description="점수가 이 값 이상인 Edge를 Seam 후보로 사용합니다",
        default=0.68,
        min=0.0,
        max=1.0,
    )
    min_chart_faces: IntProperty(
        name="최소 차트 면 수",
        description="지나치게 작은 UV 아일랜드 생성을 억제합니다",
        default=6,
        min=1,
        max=10000,
    )
    ensure_cut_paths: BoolProperty(
        name="폐곡면 절단 경로 보장",
        description="닫힌 메시가 펼쳐지도록 추가 절단 경로를 만듭니다",
        default=True,
    )
    connect_boundary_loops: BoolProperty(
        name="경계 루프 연결",
        description="여러 경계 루프를 연결해 펼치기 쉬운 차트를 만듭니다",
        default=True,
    )
    last_result: StringProperty(
        name="최근 분석",
        default="",
        options={"SKIP_SAVE"},
    )


__all__ = ("UVMAPPING_PG_settings",)
