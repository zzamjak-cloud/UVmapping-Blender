"""AI 손맵 텍스처 파이프라인의 Blender 비의존 데이터 계약과 순수 함수."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


ANALYSIS_SCHEMA_VERSION = "1.0"
DEFAULT_IMAGE_MODEL = "google/gemini-3-pro-image"
# 한 캔버스를 여러 칸으로 나눌수록 시점당 실효 픽셀이 줄어든다. 6면도 3:2 6칸은 2K에서
# 시점당 800px대, 4K에서 1600px대로 **추정**된다(Provider 실제 출력 크기를 측정하기 전의
# 대략적인 추정치이며 확정값이 아니다). 확정 전에는 이 수치를 근거로 문서를 쓰지 않는다.
IMAGE_SIZE_OPTIONS = ("1K", "2K", "4K")
# 사용자가 크기를 고르지 않았을 때 레이아웃 기본값을 쓰라는 표식.
AUTO_IMAGE_SIZE = "AUTO"
MAX_REFERENCE_IMAGE_BYTES = 32 * 1024 * 1024
REFERENCE_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# 베이크가 투영할 수 있는 직교 시점 전체. 순서는 그리드 레이아웃의 셀 순서다.
ALL_VIEWS = ("FRONT", "RIGHT", "BACK", "LEFT", "TOP", "BOTTOM")
# 프롬프트에서 각 시점을 어떻게 부를지와 카메라 배치 설명.
_VIEW_LABELS = {
    "FRONT": "FRONT",
    "RIGHT": "RIGHT SIDE",
    "BACK": "BACK",
    "LEFT": "LEFT SIDE",
    "TOP": "TOP",
    "BOTTOM": "BOTTOM",
}
_VIEW_DESCRIPTIONS = {
    "FRONT": "FRONT는 정면에서 본 모습입니다.",
    "RIGHT": "RIGHT SIDE는 모델의 오른쪽 측면에서 본 모습입니다.",
    "BACK": "BACK은 뒷면에서 본 모습입니다.",
    "LEFT": "LEFT SIDE는 모델의 왼쪽 측면에서 본 모습이며 RIGHT SIDE의 단순 좌우 반전이 아니라 실제 왼쪽 면입니다.",
    "TOP": "TOP은 모델 바로 위에서 내려다본 모습이며 정면(FRONT)이 아래쪽, 오른쪽(RIGHT)이 오른쪽에 옵니다.",
    "BOTTOM": "BOTTOM은 모델 바로 아래에서 올려다본 모습이며 정면(FRONT)이 위쪽, 오른쪽(RIGHT)이 오른쪽에 옵니다.",
}


@dataclass(frozen=True, slots=True)
class TurnaroundLayout:
    """한 장의 캔버스를 같은 크기의 정사각 셀로 나눠 시점을 배치하는 규칙.

    셀은 행 우선이며 첫 행이 캔버스 위쪽이다. 생성 요청, contact sheet 합성,
    로컬 크롭, 베이크가 모두 이 하나의 정의를 공유해야 좌표계가 어긋나지 않는다.
    """

    name: str
    views: tuple[str, ...]
    columns: int
    rows: int
    aspect_ratio: str
    # 비용과 시점당 해상도의 절충값. 6면도는 2K, 3면도는 1K로 충분하고 4K는 비용이 크므로
    # 사용자가 직접 고를 때만 쓴다. 시점당 픽셀 수의 절대값은 아직 추정이다(모듈 상단 주석 참고).
    default_image_size: str = "2K"

    def __post_init__(self) -> None:
        if self.default_image_size not in IMAGE_SIZE_OPTIONS:
            raise ValueError(f"레이아웃 기본 이미지 크기는 {', '.join(IMAGE_SIZE_OPTIONS)} 중 하나여야 합니다.")
        if self.columns <= 0 or self.rows <= 0:
            raise ValueError("레이아웃의 열과 행 수는 1 이상이어야 합니다.")
        if self.columns * self.rows != len(self.views):
            raise ValueError("레이아웃의 셀 수와 시점 수가 일치해야 합니다.")
        if len(set(self.views)) != len(self.views):
            raise ValueError("레이아웃의 시점은 중복될 수 없습니다.")
        unknown = [view for view in self.views if view not in ALL_VIEWS]
        if unknown:
            raise ValueError(f"지원하지 않는 시점입니다: {', '.join(unknown)}")

    @property
    def cell_count(self) -> int:
        return self.columns * self.rows

    def cell_index(self, view: str) -> int:
        return self.views.index(view.upper())

    def grid_cell_bounds(self, width: int, height: int, index: int) -> tuple[int, int, int, int]:
        """좌하단 원점 이미지 좌표에서 ``index``번째 셀의 (left, bottom, right, top)을 준다."""

        if not 0 <= index < self.cell_count:
            raise ValueError(f"셀 인덱스가 범위를 벗어났습니다: {index}")
        cell_width = width // self.columns
        cell_height = height // self.rows
        if cell_width < 1 or cell_height < 1:
            raise ValueError("이미지가 너무 작아 레이아웃 셀로 나눌 수 없습니다.")
        column = index % self.columns
        row_from_top = index // self.columns
        row_from_bottom = self.rows - 1 - row_from_top
        left = column * cell_width
        bottom = row_from_bottom * cell_height
        return left, bottom, left + cell_width, bottom + cell_height

    def canvas_size(self, capture_size: int) -> tuple[int, int]:
        """정사각 캡처 ``capture_size``짜리 격자를 담는 레이아웃 종횡비 캔버스 크기."""

        if capture_size <= 0:
            raise ValueError("캡처 크기는 0보다 커야 합니다.")
        grid_width = capture_size * self.columns
        grid_height = capture_size * self.rows
        aspect = aspect_value(self.aspect_ratio)
        width = max(grid_width, int(round(grid_height * aspect)))
        height = max(grid_height, int(round(grid_width / aspect)))
        return width, height

    def centered_cell_origin(
        self, width: int, height: int, index: int, content_width: int, content_height: int
    ) -> tuple[int, int]:
        """``index`` 셀 안에 ``content`` 크기 사각형을 가운데 놓을 때의 좌하단 원점.

        contact sheet 합성이 이 원점을 쓰고 로컬 크롭은 :meth:`grid_cell_bounds`를
        쓰므로, 캔버스 여백이 생기는 레이아웃에서도 두 좌표계가 같은 셀 규칙을 공유한다.
        """

        left, bottom, right, top = self.grid_cell_bounds(width, height, index)
        if content_width > right - left or content_height > top - bottom:
            raise ValueError("셀보다 큰 내용은 가운데 배치할 수 없습니다.")
        return left + (right - left - content_width) // 2, bottom + (top - bottom - content_height) // 2


def aspect_value(aspect_ratio: str) -> float:
    """"21:9" 같은 종횡비 문자열을 가로/세로 비율 값으로 바꾼다."""

    width_text, _separator, height_text = str(aspect_ratio).partition(":")
    try:
        width = float(width_text)
        height = float(height_text or "1")
    except ValueError as error:
        raise ValueError(f"올바르지 않은 종횡비입니다: {aspect_ratio}") from error
    if width <= 0.0 or height <= 0.0:
        raise ValueError(f"올바르지 않은 종횡비입니다: {aspect_ratio}")
    return width / height


TURNAROUND_LAYOUTS: Mapping[str, TurnaroundLayout] = {
    "THREE": TurnaroundLayout("THREE", ("FRONT", "RIGHT", "BACK"), 3, 1, "21:9", "1K"),
    "SIX": TurnaroundLayout("SIX", ALL_VIEWS, 3, 2, "3:2", "2K"),
}
# 한 번의 "다면도 생성"을 여러 캔버스로 쪼갤 때 쓰는 그룹 레이아웃. 그룹 하나가 캔버스 하나이자
# Provider 1회 호출이다. TURNAROUND_LAYOUTS와 이름 공간을 나눠 두어야 기존 레이아웃 enum과
# 구성(composition) 이름이 섞이지 않는다.
GROUP_LAYOUTS: Mapping[str, TurnaroundLayout] = {
    "QUAD_FRONT": TurnaroundLayout("QUAD_FRONT", ("FRONT",), 1, 1, "1:1", "2K"),
    "QUAD_BACK": TurnaroundLayout("QUAD_BACK", ("BACK",), 1, 1, "1:1", "2K"),
    "QUAD_SIDES": TurnaroundLayout("QUAD_SIDES", ("LEFT", "RIGHT"), 2, 1, "16:9", "2K"),
    "QUAD_CAPS": TurnaroundLayout("QUAD_CAPS", ("TOP", "BOTTOM"), 2, 1, "16:9", "2K"),
}
SINGLE_VIEW_LAYOUT_NAME = "SINGLE_VIEW"
SINGLE_VIEW_ASPECT_RATIO = "1:1"
# 새 생성의 기본 구성. 구버전 상태(레이아웃 기록 없음) 해석에는 쓰지 않는다.
DEFAULT_LAYOUT_NAME = "SIX"
DEFAULT_IMAGE_SIZE = TURNAROUND_LAYOUTS[DEFAULT_LAYOUT_NAME].default_image_size
# 레이아웃 이름이 없는 구버전 상태와 요청 기본값은 3열 3면도다.
LEGACY_LAYOUT_NAME = "THREE"
# 하위 호환: 3열 레이아웃의 시점 순서.
TURNAROUND_VIEWS = TURNAROUND_LAYOUTS[LEGACY_LAYOUT_NAME].views
DEFAULT_ASPECT_RATIO = TURNAROUND_LAYOUTS[LEGACY_LAYOUT_NAME].aspect_ratio
# Provider에 보낼 수 있는 종횡비는 레이아웃 계약에서만 나온다.
ASPECT_RATIO_OPTIONS = tuple(
    sorted(
        {layout.aspect_ratio for layout in TURNAROUND_LAYOUTS.values()}
        | {layout.aspect_ratio for layout in GROUP_LAYOUTS.values()}
        | {SINGLE_VIEW_ASPECT_RATIO}
    )
)


def single_view_layout(view: str) -> TurnaroundLayout:
    """순차 생성 모드에서 한 시점만 담는 1:1 레이아웃을 만든다."""

    name = str(view).upper()
    if name not in ALL_VIEWS:
        raise ValueError(f"지원하지 않는 시점입니다: {view}")
    return TurnaroundLayout(SINGLE_VIEW_LAYOUT_NAME, (name,), 1, 1, SINGLE_VIEW_ASPECT_RATIO, "1K")


def _default_image_size_source(
    layout: "TurnaroundComposition | TurnaroundLayout | str | None",
) -> "TurnaroundComposition | TurnaroundLayout":
    """기본 이미지 크기의 근거가 될 구성 또는 레이아웃을 고른다.

    ``"SIX"``처럼 구성과 레이아웃이 같은 이름을 쓰는 경우 구성을 먼저 찾지만, 구성의
    기본 크기는 첫 그룹 레이아웃에서 나오므로 기존 반환값과 동일하다.
    """

    if isinstance(layout, TurnaroundComposition):
        return layout
    if layout is not None and not isinstance(layout, TurnaroundLayout):
        name = str(layout).upper()
        if name in TURNAROUND_COMPOSITIONS:
            return TURNAROUND_COMPOSITIONS[name]
    return resolve_layout(layout)


def resolve_image_size(
    layout: "TurnaroundComposition | TurnaroundLayout | str | None", requested: str | None
) -> str:
    """요청 크기가 비어 있거나 AUTO면 구성 또는 레이아웃의 기본 크기를 돌려준다."""

    value = str(requested or "").strip().upper()
    if not value or value == AUTO_IMAGE_SIZE:
        return _default_image_size_source(layout).default_image_size
    if value not in IMAGE_SIZE_OPTIONS:
        raise ValueError(f"이미지 해상도는 {', '.join(IMAGE_SIZE_OPTIONS)} 중 하나여야 합니다.")
    return value


def resolve_layout(layout: TurnaroundLayout | str | None) -> TurnaroundLayout:
    """레이아웃 이름 또는 객체를 정규화한다. ``None``은 3열 레이아웃이다."""

    if layout is None:
        return TURNAROUND_LAYOUTS[LEGACY_LAYOUT_NAME]
    if isinstance(layout, TurnaroundLayout):
        return layout
    name = str(layout).upper()
    if name == SINGLE_VIEW_LAYOUT_NAME:
        raise ValueError("SINGLE_VIEW 레이아웃은 single_view_layout(view)로 만들어야 합니다.")
    if name in TURNAROUND_LAYOUTS:
        return TURNAROUND_LAYOUTS[name]
    if name in GROUP_LAYOUTS:
        return GROUP_LAYOUTS[name]
    if name in TURNAROUND_COMPOSITIONS and name not in TURNAROUND_LAYOUTS:
        # 여러 캔버스로 나뉘는 구성은 단일 격자가 아니므로 레이아웃으로 해석하면 안 된다.
        raise ValueError(
            f"{name}은(는) 단일 격자가 아닙니다. resolve_composition을 사용하십시오."
        )
    raise ValueError(f"지원하지 않는 3면도 레이아웃입니다: {layout}")


# 그룹 하나가 곧 Provider 1회 호출이므로 이 값이 "다면도 생성" 1번의 호출 수 상한이다.
MAX_TURNAROUND_GROUPS = 4


@dataclass(frozen=True, slots=True)
class TurnaroundComposition:
    """"다면도 생성" 1번이 만드는 그룹 묶음.

    그룹 하나 = 캔버스 하나 = Provider 1회 호출이다. 단일 캔버스 구성(THREE/SIX)도
    그룹이 하나인 구성으로 표현해 실행 경로가 같은 자료구조를 공유하게 한다.
    """

    name: str
    groups: tuple[TurnaroundLayout, ...]
    # 라운드별 그룹 이름. 뒤 라운드는 앞 라운드의 결과를 색 참조로 받는다.
    rounds: tuple[tuple[str, ...], ...]
    # 뒤 라운드가 팔레트를 맞출 기준 시점. 비우면 첫 라운드 첫 그룹의 첫 시점을 쓴다.
    color_reference_view: str = ""

    def __post_init__(self) -> None:
        if not 1 <= len(self.groups) <= MAX_TURNAROUND_GROUPS:
            raise ValueError(f"구성의 그룹 수는 1개 이상 {MAX_TURNAROUND_GROUPS}개 이하여야 합니다.")
        names = [group.name for group in self.groups]
        if len(set(names)) != len(names):
            raise ValueError("구성 안의 그룹 이름은 중복될 수 없습니다.")
        views = [view for group in self.groups for view in group.views]
        if len(set(views)) != len(views):
            raise ValueError("구성 안의 시점은 그룹 간에도 중복될 수 없습니다.")
        covered = [name for round_names in self.rounds for name in round_names]
        if sorted(covered) != sorted(names):
            raise ValueError("라운드는 모든 그룹을 정확히 한 번씩 덮어야 합니다.")
        if self.color_reference_view and self.color_reference_view not in self._first_round_views():
            raise ValueError(
                "색 기준 시점은 첫 라운드 그룹의 시점이어야 합니다: "
                f"{self.color_reference_view}"
            )

    def _first_round_views(self) -> tuple[str, ...]:
        return tuple(
            view for name in self.rounds[0] for view in self.group(name).views
        )

    @property
    def reference_view(self) -> str:
        """뒤 라운드 그룹이 색을 맞출 기준 시점. 첫 라운드에서 반드시 확보된다."""

        return self.color_reference_view or self._first_round_views()[0]

    @property
    def views(self) -> tuple[str, ...]:
        """그룹 순서대로 평탄화한 시점 목록."""

        return tuple(view for group in self.groups for view in group.views)

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(group.name for group in self.groups)

    @property
    def default_image_size(self) -> str:
        """구성의 기본 이미지 크기. 그룹별 기본값이 같으므로 첫 그룹에서 가져온다."""

        return self.groups[0].default_image_size

    @property
    def is_single_canvas(self) -> bool:
        """그룹이 하나면 기존 단일 호출 경로를 그대로 쓸 수 있다."""

        return len(self.groups) == 1

    def group(self, name: str) -> TurnaroundLayout:
        wanted = str(name).upper()
        for candidate in self.groups:
            if candidate.name == wanted:
                return candidate
        raise ValueError(f"{self.name} 구성에 없는 그룹입니다: {name}")

    def group_of_view(self, view: str) -> TurnaroundLayout:
        """시점이 속한 그룹을 돌려준다. 그룹 단위 재생성이 이 매핑을 쓴다."""

        wanted = str(view).upper()
        for candidate in self.groups:
            if wanted in candidate.views:
                return candidate
        raise ValueError(f"{self.name} 구성에 없는 시점입니다: {view}")

    def round_index(self, group_name: str) -> int:
        wanted = str(group_name).upper()
        for index, round_names in enumerate(self.rounds):
            if wanted in round_names:
                return index
        raise ValueError(f"{self.name} 구성에 없는 그룹입니다: {group_name}")


TURNAROUND_COMPOSITIONS: Mapping[str, TurnaroundComposition] = {
    "THREE": TurnaroundComposition("THREE", (TURNAROUND_LAYOUTS["THREE"],), (("THREE",),)),
    "SIX": TurnaroundComposition("SIX", (TURNAROUND_LAYOUTS["SIX"],), (("SIX",),)),
    # QUAD는 FRONT를 먼저 만들고 그 결과를 색 기준으로 삼아 나머지 3그룹을 병렬 생성한다.
    "QUAD": TurnaroundComposition(
        "QUAD",
        (
            GROUP_LAYOUTS["QUAD_FRONT"],
            GROUP_LAYOUTS["QUAD_BACK"],
            GROUP_LAYOUTS["QUAD_SIDES"],
            GROUP_LAYOUTS["QUAD_CAPS"],
        ),
        (("QUAD_FRONT",), ("QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS")),
        "FRONT",
    ),
}
# 새 생성의 기본 구성과 구버전 상태(구성 기록 없음) 해석용 구성.
DEFAULT_COMPOSITION_NAME = DEFAULT_LAYOUT_NAME
LEGACY_COMPOSITION_NAME = LEGACY_LAYOUT_NAME


def resolve_composition(
    composition: TurnaroundComposition | str | None,
) -> TurnaroundComposition:
    """구성 이름 또는 객체를 정규화한다. ``None``은 구버전 상태와 같은 3면도 구성이다."""

    if composition is None:
        return TURNAROUND_COMPOSITIONS[LEGACY_COMPOSITION_NAME]
    if isinstance(composition, TurnaroundComposition):
        return composition
    name = str(composition).strip().upper()
    if not name:
        return TURNAROUND_COMPOSITIONS[LEGACY_COMPOSITION_NAME]
    if name not in TURNAROUND_COMPOSITIONS:
        raise ValueError(f"지원하지 않는 다면도 구성입니다: {composition}")
    return TURNAROUND_COMPOSITIONS[name]


def validate_reference_image_path(path: str | Path) -> tuple[Path, str]:
    """외부 Provider로 전송하기 전에 참조 이미지 파일의 신뢰 경계를 검사한다."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"심볼릭 링크 참조 이미지는 사용할 수 없습니다: {candidate}")
    if not candidate.is_file():
        raise ValueError(f"참조 이미지를 찾을 수 없습니다: {candidate}")
    mime_type = REFERENCE_IMAGE_MIME_TYPES.get(candidate.suffix.lower())
    if mime_type is None:
        raise ValueError("참조 이미지는 JPG, PNG 또는 WebP 파일만 사용할 수 있습니다.")
    size = candidate.stat().st_size
    if size <= 0 or size > MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("참조 이미지 크기는 0바이트 초과 32MB 이하여야 합니다.")

    data = candidate.read_bytes()
    if mime_type == "image/png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    elif mime_type == "image/jpeg":
        valid = data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    else:
        valid = (
            len(data) >= 12
            and data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") == len(data) - 8
        )
    if not valid:
        raise ValueError(f"파일 내용이 올바른 {mime_type} 이미지가 아닙니다: {candidate.name}")
    return candidate, mime_type


@dataclass(frozen=True, slots=True)
class ReferenceRole:
    """각 참조 이미지에서 신뢰할 수 있는 정보의 역할."""

    reference_index: int
    role: str
    use_for: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class StyleDescription:
    """여러 참조 이미지에 공통으로 나타나는 손맵 스타일."""

    art_style: str
    brushwork: str
    detail_density: str
    outline_style: str
    color_palette: tuple[str, ...]
    shading_style: str
    edge_highlight: str


@dataclass(frozen=True, slots=True)
class SurfaceRegion:
    """모델의 특정 영역에 적용할 시각적 특징."""

    name: str
    location: str
    base_colors: tuple[str, ...]
    material_cues: tuple[str, ...]
    details: tuple[str, ...]
    confidence: float
    source: str


@dataclass(frozen=True, slots=True)
class DesignRules:
    """생성 결과에서 유지하거나 배제할 디자인 규칙."""

    preserve: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisUncertainty:
    """관찰 사실과 추론 및 참조 간 충돌을 분리한 기록."""

    observed: tuple[str, ...]
    inferred: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceAnalysis:
    """자유 형식 참조 이미지 분석의 정규화된 결과."""

    schema_version: str
    object_summary: str
    reference_roles: tuple[ReferenceRole, ...]
    style: StyleDescription
    surface_regions: tuple[SurfaceRegion, ...]
    design_rules: DesignRules
    uncertainty: AnalysisUncertainty

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화 가능한 사전으로 변환한다."""

        return asdict(self)


# 실제 모델 형상 가이드. 프롬프트의 "첫 번째 이미지"가 항상 이 역할이다.
CONTACT_SHEET_ROLE = "geometry_contact_sheet"
# 사용자가 고른 스타일 참조. 색과 재질 표현만 가져온다.
USER_REFERENCE_ROLE = "reference"
# 앞 라운드에서 만든 FRONT 완성본. 뒤 라운드의 색 기준이다.
FRONT_COLOR_REFERENCE_ROLE = "front_color_reference"
# 역할이 늘어나면 프롬프트의 입력 이미지 번호 매기기도 함께 바뀌어야 하므로 화이트리스트로 못 박는다.
INLINE_IMAGE_ROLES = (CONTACT_SHEET_ROLE, USER_REFERENCE_ROLE, FRONT_COLOR_REFERENCE_ROLE)


@dataclass(frozen=True, slots=True)
class InlineImage:
    """Provider에 전달할 base64 인라인 이미지."""

    mime_type: str
    data_base64: str
    role: str = USER_REFERENCE_ROLE
    name: str = ""

    def __post_init__(self) -> None:
        if not self.mime_type.startswith("image/"):
            raise ValueError("인라인 이미지는 image/* MIME 형식이어야 합니다.")
        if not self.data_base64.strip():
            raise ValueError("인라인 이미지 데이터가 비어 있습니다.")
        if self.role not in INLINE_IMAGE_ROLES:
            raise ValueError(
                f"인라인 이미지 역할은 {', '.join(INLINE_IMAGE_ROLES)} 중 하나여야 합니다: {self.role}"
            )


@dataclass(frozen=True, slots=True)
class TurnaroundImageRequest:
    """한 번의 호출로 한 장의 3면도만 생성하는 Provider 중립 요청."""

    prompt: str
    contact_sheet: InlineImage
    reference_images: tuple[InlineImage, ...]
    model: str = DEFAULT_IMAGE_MODEL
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    image_size: str = DEFAULT_IMAGE_SIZE
    views: tuple[str, ...] = TURNAROUND_VIEWS
    layout_name: str = LEGACY_LAYOUT_NAME
    provider_call_count: int = 1
    output_image_count: int = 1

    def __post_init__(self) -> None:
        if self.layout_name == SINGLE_VIEW_LAYOUT_NAME:
            if len(self.views) != 1:
                raise ValueError("SINGLE_VIEW 레이아웃은 시점 하나만 담습니다.")
            layout = single_view_layout(self.views[0])
        else:
            layout = resolve_layout(self.layout_name)
        if tuple(self.views) != layout.views:
            raise ValueError(
                f"{layout.name} 레이아웃의 시점 순서는 {', '.join(layout.views)}로 고정됩니다."
            )
        if self.aspect_ratio != layout.aspect_ratio:
            raise ValueError(
                f"{layout.name} 레이아웃의 종횡비는 {layout.aspect_ratio}로 고정됩니다."
            )
        if self.image_size not in IMAGE_SIZE_OPTIONS:
            raise ValueError(f"이미지 해상도는 {', '.join(IMAGE_SIZE_OPTIONS)} 중 하나여야 합니다.")
        if self.provider_call_count != 1 or self.output_image_count != 1:
            raise ValueError("비용 절감을 위해 Provider 1회 호출과 결과 1장만 허용합니다.")
        if self.contact_sheet.role != CONTACT_SHEET_ROLE:
            raise ValueError("실제 모델 contact sheet의 역할이 올바르지 않습니다.")
        # 참조 이미지는 선택 사항이다. 없으면 프롬프트만으로 스타일을 정한다.


def build_reference_analysis_prompt(reference_count: int) -> str:
    """다중 참조에서 공통 스타일을 추출하는 엄격한 JSON 프롬프트를 만든다."""

    if reference_count < 1:
        raise ValueError("참조 이미지가 최소 한 장 필요합니다.")
    return f"""당신은 캐주얼 게임용 스타일리시 손맵 텍스처 아트 디렉터입니다.
입력된 참조 이미지 {reference_count}장을 함께 분석하세요. 이미지는 같은 물체의 다른 면이 아닐 수 있으며,
각 이미지의 유용한 역할과 여러 이미지에서 반복되는 공통 스타일만 추출해야 합니다.

규칙:
- 이미지에 직접 보이는 사실은 uncertainty.observed에, 보이지 않아 추론한 내용은 uncertainty.inferred에 기록합니다.
- 참조끼리 모순되는 내용은 임의로 합치지 말고 uncertainty.conflicts에 기록합니다.
- 로고, 워터마크, 사진 배경, UI, 텍스트는 디자인 특징으로 복제하지 않습니다.
- 자세, 포즈, 손동작, 시선, 카메라 각도, 구도, 실루엣, 비율, 체형은 기록하지 않습니다. 이 값들은 텍스처를 입힐 3D 모델에서 이미 정해져 있으며, 참조에서 가져오면 결과가 어긋납니다.
- 재질의 물리적 PBR 값이 아니라 diffuse/albedo에 그릴 색, 붓질, 명암, 가장자리 강조를 설명합니다.
- Markdown, 코드펜스, 설명문 없이 아래 스키마와 정확히 같은 JSON 객체 하나만 반환합니다.
- 모든 배열은 정보가 없어도 빈 배열로 포함하고, confidence는 0.0~1.0 숫자로 반환합니다.

{{
  "schema_version": "{ANALYSIS_SCHEMA_VERSION}",
  "object_summary": "짧은 물체 및 디자인 요약(자세와 구도는 빼고 색·재질 중심으로)",
  "reference_roles": [
    {{"reference_index": 0, "role": "역할", "use_for": ["색상", "붓질"], "confidence": 0.0}}
  ],
  "style": {{
    "art_style": "스타일",
    "brushwork": "붓질",
    "detail_density": "디테일 밀도",
    "outline_style": "윤곽선",
    "color_palette": ["#RRGGBB 또는 색 이름"],
    "shading_style": "손으로 그린 명암 방식",
    "edge_highlight": "모서리 강조 방식"
  }},
  "surface_regions": [
    {{
      "name": "영역 이름",
      "location": "위치",
      "base_colors": ["색상"],
      "material_cues": ["그림으로 표현할 재질 단서"],
      "details": ["무늬 또는 마모"],
      "confidence": 0.0,
      "source": "observed 또는 inferred"
    }}
  ],
  "design_rules": {{"preserve": ["유지할 특징"], "exclude": ["배제할 특징"]}},
  "uncertainty": {{"observed": ["직접 관찰"], "inferred": ["추론"], "conflicts": ["충돌"]}}
}}"""


def _extract_json_object(raw_text: str) -> str:
    """앞뒤 잡음과 코드펜스가 있어도 첫 번째 균형 잡힌 JSON 객체를 찾는다."""

    text = raw_text.lstrip("\ufeff").strip()
    fence_matches = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates = [*fence_matches, text]
    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            character = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return candidate[start : index + 1]
    raise ValueError("응답에서 JSON 객체를 찾을 수 없습니다.")


def _remove_trailing_commas(raw_json: str) -> str:
    """문자열 내부는 보존하면서 객체와 배열 끝의 trailing comma만 제거한다."""

    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(raw_json):
        character = raw_json[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == ",":
            next_index = index + 1
            while next_index < len(raw_json) and raw_json[next_index].isspace():
                next_index += 1
            if next_index < len(raw_json) and raw_json[next_index] in "}]":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(text for item in value if (text := _text(item)))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def normalize_reference_analysis(payload: Mapping[str, Any]) -> ReferenceAnalysis:
    """알 수 없는 필드를 버리고 예상 타입과 허용값으로 분석 결과를 정규화한다."""

    if not isinstance(payload, Mapping):
        raise ValueError("분석 결과의 최상위 값은 JSON 객체여야 합니다.")
    style = _mapping(payload.get("style"))
    rules = _mapping(payload.get("design_rules"))
    uncertainty = _mapping(payload.get("uncertainty"))

    reference_roles: list[ReferenceRole] = []
    raw_roles = payload.get("reference_roles")
    if isinstance(raw_roles, (list, tuple)):
        for position, item in enumerate(raw_roles):
            role = _mapping(item)
            raw_index = role.get("reference_index", position)
            try:
                reference_index = max(0, int(raw_index))
            except (TypeError, ValueError):
                reference_index = position
            reference_roles.append(
                ReferenceRole(
                    reference_index=reference_index,
                    role=_text(role.get("role")),
                    use_for=_text_tuple(role.get("use_for")),
                    confidence=_confidence(role.get("confidence")),
                )
            )

    surface_regions: list[SurfaceRegion] = []
    raw_regions = payload.get("surface_regions")
    if isinstance(raw_regions, (list, tuple)):
        for item in raw_regions:
            region = _mapping(item)
            source = _text(region.get("source")).lower()
            if source not in {"observed", "inferred"}:
                source = "inferred"
            surface_regions.append(
                SurfaceRegion(
                    name=_text(region.get("name")),
                    location=_text(region.get("location")),
                    base_colors=_text_tuple(region.get("base_colors")),
                    material_cues=_text_tuple(region.get("material_cues")),
                    details=_text_tuple(region.get("details")),
                    confidence=_confidence(region.get("confidence")),
                    source=source,
                )
            )

    return ReferenceAnalysis(
        schema_version=ANALYSIS_SCHEMA_VERSION,
        object_summary=_text(payload.get("object_summary")),
        reference_roles=tuple(reference_roles),
        style=StyleDescription(
            art_style=_text(style.get("art_style")),
            brushwork=_text(style.get("brushwork")),
            detail_density=_text(style.get("detail_density")),
            outline_style=_text(style.get("outline_style")),
            color_palette=_text_tuple(style.get("color_palette")),
            shading_style=_text(style.get("shading_style")),
            edge_highlight=_text(style.get("edge_highlight")),
        ),
        surface_regions=tuple(surface_regions),
        design_rules=DesignRules(
            preserve=_text_tuple(rules.get("preserve")),
            exclude=_text_tuple(rules.get("exclude")),
        ),
        uncertainty=AnalysisUncertainty(
            observed=_text_tuple(uncertainty.get("observed")),
            inferred=_text_tuple(uncertainty.get("inferred")),
            conflicts=_text_tuple(uncertainty.get("conflicts")),
        ),
    )


def parse_reference_analysis(raw_text: str) -> ReferenceAnalysis:
    """모델의 느슨한 텍스트 응답을 정규화된 참조 분석으로 변환한다."""

    try:
        payload = json.loads(_remove_trailing_commas(_extract_json_object(raw_text)))
    except json.JSONDecodeError as error:
        raise ValueError(f"참조 분석 JSON을 해석할 수 없습니다: {error.msg}") from error
    if not isinstance(payload, Mapping):
        raise ValueError("참조 분석 JSON은 객체여야 합니다.")
    return normalize_reference_analysis(payload)


# FRONT 색 참조가 끼면 사용자 참조는 세 번째 이미지부터 시작한다. 번호가 어긋나면
# 모델이 참조 역할을 뒤바꿔 해석하므로 한 곳에서만 만들어 쓴다.
_FRONT_REFERENCE_ROLE_LINE = (
    f"- 두 번째 이미지(role={FRONT_COLOR_REFERENCE_ROLE}): 같은 모델의 FRONT 시점 완성본입니다. "
    "색과 재질의 기준으로만 사용하고 형상은 첫 번째 이미지를 따릅니다."
)


def _style_sections(
    analysis: ReferenceAnalysis | Mapping[str, Any] | None,
    user_prompt: str,
    reference_image_count: int,
    guide_description: str,
    *,
    front_reference: bool = False,
) -> tuple[str, str]:
    """입력 이미지 역할 문단과 스타일 근거 문단을 만든다.

    ``front_reference``가 참이면 두 번째 이미지가 FRONT 완성본이므로 사용자 참조의
    번호를 "세 번째 이후"로 한 칸 밀어 적는다.
    """

    if reference_image_count and analysis is None:
        raise ValueError("참조 이미지를 함께 보낼 때는 참조 분석이 필요합니다.")
    if analysis is None:
        if not user_prompt.strip():
            raise ValueError("참조 분석이 없으면 사용자 지시가 필요합니다.")
        if front_reference:
            role_section = (
                f"- 첫 번째 이미지(role={CONTACT_SHEET_ROLE}): {guide_description} "
                "실루엣, 비율, 부품 배치를 반드시 이 이미지에 맞춥니다.\n"
                f"{_FRONT_REFERENCE_ROLE_LINE}"
            )
            style_section = (
                "스타일 근거:\n"
                "- 참조 이미지가 없습니다. 아래 사용자 지시와 두 번째 이미지의 색을 색, 재질 표현, "
                "분위기의 근거로 사용합니다.\n"
                "- 지시가 다루지 않는 부분은 깔끔한 캐주얼 게임 손맵 스타일로 절제해 표현합니다."
            )
            return role_section, style_section
        role_section = (
            f"- 첫 번째 이미지(role=geometry_contact_sheet): {guide_description} "
            "실루엣, 비율, 부품 배치를 반드시 이 이미지에 맞춥니다. 이 외의 입력 이미지는 없습니다."
        )
        style_section = (
            "스타일 근거:\n"
            "- 참조 이미지가 없습니다. 아래 사용자 지시를 색, 재질 표현, 분위기의 유일한 근거로 사용합니다.\n"
            "- 지시가 다루지 않는 부분은 깔끔한 캐주얼 게임 손맵 스타일로 절제해 표현합니다."
        )
        return role_section, style_section

    normalized = (
        analysis
        if isinstance(analysis, ReferenceAnalysis)
        else normalize_reference_analysis(analysis)
    )
    # reference_roles와 uncertainty는 분석 단계의 기록일 뿐이고, 참조의
    # 서술이 생성 형상으로 새는 통로가 된다. 스타일 근거만 싣는다.
    analysis_payload = {
        key: value
        for key, value in normalized.to_dict().items()
        if key in ("object_summary", "style", "surface_regions", "design_rules")
    }
    analysis_json = json.dumps(analysis_payload, ensure_ascii=False, separators=(",", ":"))
    if front_reference:
        role_section = (
            f"- 첫 번째 이미지(role={CONTACT_SHEET_ROLE}): {guide_description} "
            "실루엣, 비율, 자세, 부품 배치를 반드시 이 이미지에 맞춥니다.\n"
            f"{_FRONT_REFERENCE_ROLE_LINE}"
        )
        if reference_image_count:
            role_section += (
                f"\n- 세 번째 이후 이미지 {reference_image_count}장(role=palette_only): "
                "색과 재질 표현만 참고합니다. "
                "이 이미지의 캐릭터, 비율, 자세, 실루엣, 부품 구성은 절대 가져오지 않습니다."
            )
        return role_section, f"분석 JSON:\n{analysis_json}"
    if reference_image_count:
        role_section = (
            f"- 첫 번째 이미지(role=geometry_contact_sheet): {guide_description} "
            "실루엣, 비율, 자세, 부품 배치를 반드시 이 이미지에 맞춥니다.\n"
            f"- 두 번째 이후 이미지 {reference_image_count}장(role=palette_only): 색과 재질 표현만 참고합니다. "
            "이 이미지의 캐릭터, 비율, 자세, 실루엣, 부품 구성은 절대 가져오지 않습니다."
        )
    else:
        role_section = (
            "- 입력 이미지는 첫 번째 한 장뿐입니다(role=geometry_contact_sheet). "
            f"{guide_description} 실루엣, 비율, 자세, 부품 배치를 반드시 이 이미지에 맞춥니다.\n"
            "- 스타일은 아래 분석 JSON 텍스트만 근거로 합니다."
        )
    return role_section, f"분석 JSON:\n{analysis_json}"


def _shape_contract(guide_line: str) -> str:
    """다른 모든 지시보다 우선하는 형상 계약 문단."""

    # 자세 부위를 열거하면 이미지 모델이 오히려 그 동작을 만들어 내므로
    # 실루엣 밖에 아무것도 그리지 말라는 조건만 둔다.
    return (
        "형상 계약(다른 모든 지시보다 우선):\n"
        "- 이 작업은 새 그림을 그리는 것이 아니라 첫 번째 입력 이미지 위에 픽셀 단위로 정렬된 채색(paint-over)입니다. "
        "실루엣 변경 금지. 물체를 옮기거나 키우거나 줄이거나 회전하지 않습니다.\n"
        f"- {guide_line} 그 실루엣 안쪽을 색으로 채우는 작업이며, 실루엣 밖에는 아무것도 그리지 않습니다.\n"
        "- 첫 번째 이미지의 실루엣, 비율, 크기, 화면 안 위치를 그대로 유지합니다. "
        "이 형상은 텍스처를 입힐 실제 3D 모델이므로 한 픽셀도 재해석하지 않습니다.\n"
        "- 첫 번째 이미지에 없는 부품(가방, 소품, 장비, 장식)은 추가하지 않고, 있는 부품을 빼지도 않습니다.\n"
        "- 다른 참조나 분석 결과가 이 계약과 충돌하면 언제나 첫 번째 이미지를 따릅니다."
    )


def _common_output_rules() -> str:
    """레이아웃과 무관하게 모든 생성 결과에 적용하는 출력 규칙."""

    return (
        "- 원근을 제거한 orthographic view처럼 표현하고 물체가 잘리지 않게 충분한 여백을 둡니다.\n"
        "- 조명 사진이나 렌더가 아니라 diffuse/albedo에 옮길 수 있는 손으로 그린 색과 명암을 표현합니다. "
        "강한 그림자, 하이라이트, 반사는 넣지 않습니다.\n"
        "- 배경은 투명 또는 완전히 균일한 단색으로 만듭니다.\n"
        "- 텍스트, 라벨, 구분선, 숫자, 로고, 워터마크, 받침대, 그림자는 넣지 않습니다."
    )


def _front_color_reference_section() -> str:
    """앞 라운드의 FRONT 완성본을 색 기준으로 삼으라는 문단.

    형상은 언제나 가이드(첫 번째 이미지)가 이기고, 색만 FRONT 완성본을 따른다.
    캔버스를 여러 장으로 쪼개면 한 캔버스 안에서 모델이 스스로 맞춰 주던 색 일관성이
    사라지므로, 그 보장을 프롬프트로 대신 세운다.
    """

    return (
        "FRONT 색 기준(형상 계약 다음으로 우선):\n"
        f"- 두 번째 입력 이미지(role={FRONT_COLOR_REFERENCE_ROLE})는 같은 모델의 FRONT 시점 완성본입니다.\n"
        "- 팔레트, 각 부위의 색상과 명도, 무늬 밀도, 부위 경계의 색을 이 이미지와 정확히 일치시킵니다.\n"
        "- 다만 형상·실루엣·배치는 여전히 첫 번째 이미지(가이드)를 따릅니다. "
        "두 이미지가 충돌하면 형상은 첫 번째, 색은 두 번째를 따릅니다."
    )


def _layout_output_contract(layout: TurnaroundLayout) -> str:
    """그리드 레이아웃별 캔버스 분할과 시점 순서 지시."""

    if layout.cell_count == 1:
        # 셀이 하나면 "같은 너비의 1열로 나눕니다"가 어색하므로 단일 캔버스 문구를 쓴다.
        # 순차 모드의 인페인팅 문장은 넣지 않는다. 이 경로에는 이미 칠해진 영역이 없다.
        single_descriptions = " ".join(
            _VIEW_DESCRIPTIONS[view] for view in layout.views if view not in ("FRONT", "BACK")
        )
        return (
            f"- 하나의 {layout.aspect_ratio} 캔버스에 시점 하나만 담습니다. "
            f"첫 번째 입력 이미지가 정확히 같은 {layout.aspect_ratio} 단일 캔버스이므로 "
            "그 레이아웃을 그대로 따릅니다.\n"
            "- 캔버스 중앙 정사각형 viewport 안에 물체를 배치하고, "
            "viewport의 상하좌우 여백을 동일하게 유지합니다.\n"
            + (f"- {single_descriptions}\n" if single_descriptions else "")
            + "- 첫 번째 입력 이미지의 실루엣 위에 색만 덧입히듯, 물체의 외곽선 위치, 크기, 중심을 "
            "입력과 최대한 일치시킵니다. "
            "부위 경계(예: 장갑과 소매, 신발과 바지)도 입력 실루엣의 같은 위치에 맞춥니다."
        )

    row_labels = []
    for row in range(layout.rows):
        views = layout.views[row * layout.columns : (row + 1) * layout.columns]
        row_labels.append(" | ".join(_VIEW_LABELS[view] for view in views))
    if layout.rows == 1:
        split = f"하나의 {layout.aspect_ratio} 캔버스를 같은 너비의 {layout.columns}열로 나눕니다."
        order = f"- 왼쪽부터 {row_labels[0]} 순서이며, "
    else:
        split = (
            f"하나의 {layout.aspect_ratio} 캔버스를 같은 너비의 {layout.columns}열과 "
            f"같은 높이의 {layout.rows}행, 총 {layout.cell_count}칸으로 나눕니다."
        )
        rows_text = " / 아랫줄은 ".join(row_labels)
        order = f"- 윗줄은 왼쪽부터 {rows_text} 순서이며, "
    descriptions = " ".join(_VIEW_DESCRIPTIONS[view] for view in layout.views if view not in ("FRONT", "BACK"))
    count_word = "모든" if layout.cell_count > 1 else "그"
    return (
        f"- {split} 첫 번째 입력 이미지가 정확히 같은 {layout.aspect_ratio} "
        f"{layout.columns}열{'' if layout.rows == 1 else f' {layout.rows}행'} 배치이므로 그 레이아웃을 그대로 따릅니다.\n"
        f"{order}{count_word} 칸에 동일한 물체, 동일한 축척, 동일한 중심을 배치합니다.\n"
        f"- 각 칸의 중앙 정사각형 viewport 안에 물체를 배치하고, {count_word} viewport의 상하좌우 여백을 동일하게 유지합니다.\n"
        + (f"- {descriptions}\n" if descriptions else "")
        + "- 첫 번째 입력 이미지의 실루엣 위에 색만 덧입히듯, 각 칸에서 물체의 외곽선 위치, 크기, 중심을 입력과 최대한 일치시킵니다. "
        "부위 경계(예: 장갑과 소매, 신발과 바지)도 입력 실루엣의 같은 위치에 맞춥니다.\n"
        f"- {count_word} 시점의 색, 무늬, 마모, 부품 연결은 서로 연속되고 일관되어야 합니다. "
        "같은 부위는 어느 시점에서 보아도 같은 색과 명도로 칠합니다."
        # 두 칸짜리 캔버스는 모델이 두 시점을 하나의 넓은 그림으로 합칠 위험이 가장 크다.
        + (
            "\n- 두 칸 사이에 구분선, 테두리, 배경색 차이를 넣지 않습니다. "
            "두 칸은 같은 배경 위의 서로 다른 viewport이며, 같은 물체를 두 방향에서 본 모습입니다. "
            "두 칸을 하나의 넓은 그림으로 합치지 마십시오."
            if layout.cell_count == 2
            else ""
        )
    )


def _regeneration_feedback_section(feedback_views: Sequence[str]) -> str:
    """이전 시도에서 실루엣 내부 구조가 어긋난 시점을 마스크 계약으로 다시 못 박는다.

    자세 부위를 열거하면 이미지 모델이 그 동작을 새로 만들어 내므로, 어디가
    배경이고 어디가 회색인지의 관계만 말한다.
    """

    views = tuple(str(view).upper() for view in feedback_views if str(view).strip())
    if not views:
        return ""
    labels = ", ".join(_VIEW_LABELS.get(view, view) for view in views)
    return (
        "이전 시도 교정:\n"
        f"- 이전 결과에서 {labels} 시점의 실루엣 내부 구조가 가이드와 달랐습니다.\n"
        "- 가이드에서 배경이 보이는 모든 틈(부품과 부품 사이 등)은 결과에서도 반드시 같은 위치에 배경으로 남기고, "
        "가이드의 회색 영역은 빠짐없이 채색하십시오.\n"
        "- 부품을 서로 붙이거나 틈을 메우거나 새로 벌리지 마십시오. 회색인 곳만 색이 있고, 흰 곳은 흰 채로 둡니다.\n\n"
    )


def compile_turnaround_prompt(
    analysis: ReferenceAnalysis | Mapping[str, Any] | None,
    user_prompt: str = "",
    *,
    reference_image_count: int = 0,
    layout: TurnaroundLayout | str | None = None,
    regeneration_feedback: Sequence[str] = (),
    front_reference: bool = False,
) -> str:
    """모델 형상과 참조 스타일(또는 프롬프트만)을 한 장의 그리드 시점도에 결합하도록 지시한다.

    ``analysis``가 ``None``이면 참조 이미지 없이 사용자 지시만으로 스타일을
    정하는 프롬프트를 만든다. 이때 사용자 지시는 비어 있으면 안 된다.
    ``layout``이 ``None``이면 3열 21:9 레이아웃이다. ``regeneration_feedback``에
    시점 이름이 있으면 그 시점의 실루엣 불일치를 교정하는 문단을 덧붙인다.
    ``front_reference``가 참이면 두 번째 입력 이미지가 앞 라운드의 FRONT 완성본이라고
    보고 색 기준 문단을 넣고 사용자 참조 번호를 한 칸 민다.
    """

    resolved_layout = resolve_layout(layout)
    instruction = user_prompt.strip() or "추가 지시 없음"
    feedback_section = _regeneration_feedback_section(regeneration_feedback)
    role_section, style_section = _style_sections(
        analysis,
        user_prompt,
        reference_image_count,
        "Blender의 실제 모델 형상입니다.",
        front_reference=front_reference,
    )
    front_section = f"\n{_front_color_reference_section()}\n" if front_reference else ""
    view_count = len(resolved_layout.views)
    if view_count == 1:
        title_noun = "단일 시점도"
    elif view_count == 3:
        title_noun = "3면도"
    else:
        title_noun = f"{view_count}시점도"
    return f"""캐주얼 게임용 스타일리시 손맵 diffuse/albedo 제작을 위한 {title_noun} 한 장을 생성하세요.

입력 이미지 역할:
{role_section}

{_shape_contract("첫 번째 이미지는 흰 배경 위의 회색 3D 모델입니다.")}
{front_section}
{feedback_section}{style_section}

사용자 한 줄 지시:
{instruction}

출력 계약:
- Provider 호출 한 번에서 최종 이미지 정확히 한 장만 생성합니다. 시점별로 여러 번 생성하지 마세요.
{_layout_output_contract(resolved_layout)}
{_common_output_rules()}
- 관찰되지 않은 뒷면은 주어진 근거(분석 JSON 또는 사용자 지시)와 일관되게 절제해 표현하며 새 부품을 임의로 만들지 않습니다."""


def compile_sequential_view_prompt(
    analysis: ReferenceAnalysis | Mapping[str, Any] | None,
    user_prompt: str = "",
    *,
    view: str,
    painted_views: tuple[str, ...] = (),
    reference_image_count: int = 0,
) -> str:
    """순차 생성 모드에서 한 시점을 채색(또는 미채색 영역만 보완)하도록 지시한다.

    ``painted_views``가 비어 있으면 회색 실루엣 전체를 채색하고, 있으면 이미
    투영된 색을 유지한 채 회색으로 남은 영역만 인접 색과 이어지게 채운다.
    """

    layout = single_view_layout(view)
    view_name = layout.views[0]
    instruction = user_prompt.strip() or "추가 지시 없음"
    painted = tuple(str(item).upper() for item in painted_views)
    if painted:
        guide_description = (
            f"이미 일부가 채색된 실제 3D 모델을 {_VIEW_LABELS[view_name]}에서 본 모습입니다."
        )
        guide_line = (
            f"첫 번째 이미지는 이미 일부가 채색된 모델을 {_VIEW_LABELS[view_name]}에서 본 모습입니다. "
            f"색이 있는 영역은 {', '.join(_VIEW_LABELS[item] for item in painted)} 시점에서 확정된 텍스처이므로 한 픽셀도 바꾸지 않고, "
            "회색(미채색) 영역만 인접한 채색 영역과 색·무늬·명암이 이어지도록 채웁니다."
        )
        task_line = (
            f"이 시점({_VIEW_LABELS[view_name]})에서 회색으로 남은 미채색 영역만 채색하세요. "
            "이미 색이 있는 영역은 그대로 보존합니다."
        )
    else:
        guide_description = f"흰 배경 위 회색 실루엣으로 렌더한 실제 3D 모델을 {_VIEW_LABELS[view_name]}에서 본 모습입니다."
        guide_line = (
            f"첫 번째 이미지는 흰 배경 위의 회색 3D 모델을 {_VIEW_LABELS[view_name]}에서 본 모습입니다."
        )
        task_line = f"이 시점({_VIEW_LABELS[view_name]})의 회색 실루엣 전체를 채색하세요."
    role_section, style_section = _style_sections(
        analysis, user_prompt, reference_image_count, guide_description
    )
    return f"""캐주얼 게임용 스타일리시 손맵 diffuse/albedo 제작을 위한 단일 시점 채색 한 장을 생성하세요.
{task_line}

입력 이미지 역할:
{role_section}

{_shape_contract(guide_line)}

시점 설명:
- {_VIEW_DESCRIPTIONS[view_name]}

{style_section}

사용자 한 줄 지시:
{instruction}

출력 계약:
- Provider 호출 한 번에서 최종 이미지 정확히 한 장만 생성합니다.
- 하나의 1:1 캔버스에 시점 하나만 담습니다. 첫 번째 입력 이미지와 같은 위치, 같은 크기로 물체를 배치합니다.
{_common_output_rules()}
- 관찰되지 않은 면은 주어진 근거(분석 JSON 또는 사용자 지시)와 이미 채색된 영역에 일관되게 절제해 표현하며 새 부품을 임의로 만들지 않습니다."""


def build_turnaround_request(
    contact_sheet: InlineImage,
    reference_images: Sequence[InlineImage],
    analysis: ReferenceAnalysis | Mapping[str, Any] | None,
    user_instruction: str = "",
    *,
    model: str = DEFAULT_IMAGE_MODEL,
    layout_name: str = LEGACY_LAYOUT_NAME,
    image_size: str = DEFAULT_IMAGE_SIZE,
    view: str | None = None,
    painted_views: tuple[str, ...] = (),
    regeneration_feedback: Sequence[str] = (),
    front_reference: bool = False,
) -> TurnaroundImageRequest:
    """비용 계약이 고정된 단일 이미지 요청을 만든다.

    ``layout_name``이 SINGLE_VIEW면 ``view``가 필수이며 순차 생성용 프롬프트를 쓴다.
    ``regeneration_feedback``은 그리드 레이아웃 재생성 때 불일치 시점 이름을 넘긴다.
    """

    if layout_name.upper() == SINGLE_VIEW_LAYOUT_NAME:
        if not view:
            raise ValueError("SINGLE_VIEW 레이아웃에는 시점(view)이 필요합니다.")
        layout = single_view_layout(view)
        prompt = compile_sequential_view_prompt(
            analysis,
            user_instruction,
            view=layout.views[0],
            painted_views=painted_views,
            reference_image_count=len(reference_images),
        )
    else:
        layout = resolve_layout(layout_name)
        palette_reference_count = len(reference_images) - (1 if front_reference else 0)
        if palette_reference_count < 0:
            raise ValueError("FRONT 색 참조를 쓰려면 참조 이미지 목록의 첫 장이 그 참조여야 합니다.")
        prompt = compile_turnaround_prompt(
            analysis,
            user_instruction,
            reference_image_count=palette_reference_count,
            layout=layout,
            regeneration_feedback=regeneration_feedback,
            front_reference=front_reference,
        )
    return TurnaroundImageRequest(
        prompt=prompt,
        contact_sheet=contact_sheet,
        reference_images=tuple(reference_images),
        model=model,
        aspect_ratio=layout.aspect_ratio,
        image_size=image_size,
        views=layout.views,
        layout_name=layout.name,
    )


@dataclass(frozen=True, slots=True)
class TurnaroundBatchRequest:
    """구성의 그룹마다 요청 하나를 담은, 비용 계약 검증을 통과한 묶음.

    그룹 하나 = 캔버스 하나 = Provider 1회 호출 = 결과 1장이라는 기존 계약을 그대로
    유지하면서, "다면도 생성" 1번의 총 호출 수만 위에서 한 번 더 제한한다.
    """

    composition_name: str
    requests: tuple[TurnaroundImageRequest, ...]

    def __post_init__(self) -> None:
        composition = resolve_composition(self.composition_name)
        if len(self.requests) != len(composition.groups):
            raise ValueError(
                f"{composition.name} 구성은 그룹 {len(composition.groups)}개의 요청이 모두 필요합니다."
            )
        if len(self.requests) > MAX_TURNAROUND_GROUPS:
            raise ValueError(f"한 번의 생성은 최대 {MAX_TURNAROUND_GROUPS}개 그룹까지만 허용합니다.")
        for request, group in zip(self.requests, composition.groups):
            if request.layout_name != group.name:
                raise ValueError(
                    f"요청 순서가 구성의 그룹 순서와 달라집니다: {request.layout_name} != {group.name}"
                )
        call_count = sum(request.provider_call_count for request in self.requests)
        image_count = sum(request.output_image_count for request in self.requests)
        if call_count != len(self.requests) or call_count > MAX_TURNAROUND_GROUPS:
            raise ValueError(
                f"그룹당 Provider 1회 호출만 허용하며 총 호출은 {MAX_TURNAROUND_GROUPS}회를 넘을 수 없습니다."
            )
        if image_count != len(self.requests):
            raise ValueError("그룹당 결과 이미지 1장만 허용합니다.")
        views = tuple(view for request in self.requests for view in request.views)
        if len(set(views)) != len(views):
            raise ValueError("그룹 간에 같은 시점이 중복될 수 없습니다.")
        if views != composition.views:
            raise ValueError(
                f"{composition.name} 구성의 시점 목록과 일치해야 합니다: {', '.join(composition.views)}"
            )

    @property
    def composition(self) -> TurnaroundComposition:
        return resolve_composition(self.composition_name)

    @property
    def views(self) -> tuple[str, ...]:
        return tuple(view for request in self.requests for view in request.views)

    @property
    def provider_call_count(self) -> int:
        return sum(request.provider_call_count for request in self.requests)

    @property
    def rounds(self) -> tuple[tuple[int, ...], ...]:
        """구성의 라운드를 ``requests`` 인덱스로 옮긴 것."""

        index_by_name = {request.layout_name: index for index, request in enumerate(self.requests)}
        return tuple(
            tuple(index_by_name[name] for name in round_names)
            for round_names in self.composition.rounds
        )

    def request_for(self, group_name: str) -> TurnaroundImageRequest:
        wanted = str(group_name).upper()
        for request in self.requests:
            if request.layout_name == wanted:
                return request
        raise ValueError(f"{self.composition_name} 묶음에 없는 그룹입니다: {group_name}")


def build_turnaround_batch_request(
    composition: TurnaroundComposition | str | None,
    contact_sheets: Mapping[str, InlineImage],
    reference_images: Sequence[InlineImage] = (),
    analysis: ReferenceAnalysis | Mapping[str, Any] | None = None,
    user_instruction: str = "",
    *,
    model: str = DEFAULT_IMAGE_MODEL,
    image_size: str = AUTO_IMAGE_SIZE,
    front_reference: InlineImage | None = None,
    regeneration_feedback: Mapping[str, Sequence[str]] | None = None,
) -> TurnaroundBatchRequest:
    """구성의 그룹마다 요청 하나씩을 만들어 묶는다.

    ``contact_sheets``는 그룹 이름 → 그 그룹 배치로 합성한 형상 가이드다.
    ``front_reference``는 라운드 0의 FRONT 결과이며, 있으면 라운드 1 이상의 그룹에만
    두 번째 입력 이미지로 붙는다. 라운드 0을 아직 실행하지 않아 FRONT 결과가 없을 때는
    ``None``으로 두고, 라운드 1을 띄울 때 이 함수를 다시 불러 참조를 채운다.
    """

    resolved = resolve_composition(composition)
    if front_reference is not None and front_reference.role != FRONT_COLOR_REFERENCE_ROLE:
        raise ValueError(f"FRONT 색 참조의 역할은 {FRONT_COLOR_REFERENCE_ROLE}이어야 합니다.")
    feedback = {
        str(key).upper(): tuple(value) for key, value in (regeneration_feedback or {}).items()
    }
    unknown_feedback = sorted(set(feedback) - set(resolved.group_names))
    if unknown_feedback:
        raise ValueError(f"{resolved.name} 구성에 없는 그룹입니다: {', '.join(unknown_feedback)}")

    user_references = tuple(reference_images)
    requests: list[TurnaroundImageRequest] = []
    for group in resolved.groups:
        sheet = contact_sheets.get(group.name)
        if sheet is None:
            raise ValueError(f"{group.name} 그룹의 형상 가이드 이미지가 없습니다.")
        use_front = front_reference is not None and resolved.round_index(group.name) > 0
        group_references = (front_reference, *user_references) if use_front else user_references
        requests.append(
            build_turnaround_request(
                sheet,
                group_references,
                analysis,
                user_instruction,
                model=model,
                layout_name=group.name,
                image_size=resolve_image_size(group, image_size),
                regeneration_feedback=feedback.get(group.name, ()),
                front_reference=use_front,
            )
        )
    return TurnaroundBatchRequest(resolved.name, tuple(requests))
