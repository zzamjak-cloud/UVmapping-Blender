"""자동 Seam 분석의 공개 데이터 타입.

이 모듈은 Blender를 import하지 않으므로 일반 Python 테스트에서도 사용할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import radians
from typing import Any, ClassVar


class AnalysisPreset(str, Enum):
    """메시 성격에 맞춘 Seam 분석 프리셋."""

    ORGANIC = "ORGANIC"
    BALANCED = "BALANCED"
    HARD_SURFACE = "HARD_SURFACE"

    @classmethod
    def coerce(cls, value: "AnalysisPreset | str") -> "AnalysisPreset":
        """UI 친화적인 문자열을 정규화해 프리셋으로 변환한다."""

        if isinstance(value, cls):
            return value
        normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        try:
            return cls(normalized)
        except ValueError as exc:
            choices = ", ".join(member.value for member in cls)
            raise ValueError(f"지원하지 않는 분석 프리셋입니다: {value!r} ({choices})") from exc


@dataclass(frozen=True, slots=True)
class AnalysisOptions:
    """자동 Seam 후보 생성 설정.

    직접 생성하면 Balanced 값이 사용된다. 다른 프리셋은
    :meth:`for_preset`으로 생성해야 프리셋별 가중치가 함께 적용된다.
    """

    preset: AnalysisPreset = AnalysisPreset.BALANCED
    angle_weight: float = 1.8
    concave_multiplier: float = 1.25
    length_weight: float = 0.4
    sharp_weight: float = 2.2
    material_weight: float = 1.6
    existing_seam_weight: float = 4.0
    boundary_weight: float = 4.0
    non_manifold_weight: float = 5.0
    seam_threshold: float = 0.68
    angle_reference: float = radians(50.0)
    min_chart_faces: int = 4
    preserve_existing_seams: bool = True
    ensure_cut_paths: bool = True
    connect_boundary_loops: bool = True

    _PRESETS: ClassVar[dict[AnalysisPreset, dict[str, Any]]] = {
        AnalysisPreset.ORGANIC: {
            "angle_weight": 1.25,
            "concave_multiplier": 1.7,
            "length_weight": 0.55,
            "sharp_weight": 1.4,
            "material_weight": 1.2,
            "existing_seam_weight": 4.0,
            "boundary_weight": 4.0,
            "non_manifold_weight": 5.0,
            "seam_threshold": 0.66,
            "angle_reference": radians(70.0),
            "min_chart_faces": 6,
        },
        AnalysisPreset.BALANCED: {
            "angle_weight": 1.8,
            "concave_multiplier": 1.25,
            "length_weight": 0.4,
            "sharp_weight": 2.2,
            "material_weight": 1.6,
            "existing_seam_weight": 4.0,
            "boundary_weight": 4.0,
            "non_manifold_weight": 5.0,
            "seam_threshold": 0.68,
            "angle_reference": radians(50.0),
            "min_chart_faces": 4,
        },
        AnalysisPreset.HARD_SURFACE: {
            "angle_weight": 2.3,
            "concave_multiplier": 1.1,
            "length_weight": 0.3,
            "sharp_weight": 3.2,
            "material_weight": 2.4,
            "existing_seam_weight": 4.5,
            "boundary_weight": 4.0,
            "non_manifold_weight": 5.0,
            "seam_threshold": 0.72,
            "angle_reference": radians(35.0),
            "min_chart_faces": 3,
        },
    }

    def __post_init__(self) -> None:
        object.__setattr__(self, "preset", AnalysisPreset.coerce(self.preset))
        non_negative = (
            "angle_weight",
            "concave_multiplier",
            "length_weight",
            "sharp_weight",
            "material_weight",
            "existing_seam_weight",
            "boundary_weight",
            "non_manifold_weight",
        )
        for name in non_negative:
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} 값은 0 이상이어야 합니다.")
        if not 0.0 <= self.seam_threshold <= 1.0:
            raise ValueError("seam_threshold 값은 0과 1 사이여야 합니다.")
        if self.angle_reference <= 0.0:
            raise ValueError("angle_reference 값은 0보다 커야 합니다.")
        if self.min_chart_faces < 1:
            raise ValueError("min_chart_faces 값은 1 이상이어야 합니다.")

    @classmethod
    def for_preset(
        cls,
        preset: AnalysisPreset | str,
        **overrides: Any,
    ) -> "AnalysisOptions":
        """프리셋 기본값에 선택적인 사용자 재정의를 적용한다."""

        normalized = AnalysisPreset.coerce(preset)
        values = dict(cls._PRESETS[normalized])
        values.update(overrides)
        return cls(preset=normalized, **values)


@dataclass(slots=True)
class AnalysisResult:
    """메시를 변경하지 않고 반환하는 Seam 분석 결과."""

    seam_edges: set[int] = field(default_factory=set)
    edge_scores: dict[int, float] = field(default_factory=dict)
    chart_count: int = 0
    warnings: list[str] = field(default_factory=list)
    options: AnalysisOptions | None = None
    candidate_label: str = ""
