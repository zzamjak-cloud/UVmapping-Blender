"""Blender 없이 실행하는 시점 간 띠 높이 맞춤 테스트."""

from __future__ import annotations

from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.band_align import best_shift_index, shift_candidates, solve_view_shifts
from uvmapping.texture_bake import BakeTriangle, build_raster_source, rasterize_atlas


BASE = (0.85, 0.45, 0.25, 1.0)  # 바탕색. 밝은 무채색은 배경으로 판정되므로 주황 계열로 둔다.
DARK = (0.15, 0.15, 0.2, 1.0)


def test_candidates_start_at_zero_and_alternate() -> None:
    values = shift_candidates(2.0)
    assert values[0] == 0.0
    assert values[1] == -values[2] > 0.0
    assert max(values) <= 2.0 * 0.06 + 1.0e-9


def test_best_shift_index_finds_offset_stripe() -> None:
    # 기준: 10칸 중 4~5번에 어두운 띠. 후보 k는 다른 그림을 k칸 옮겨 읽은 결과다.
    reference = [DARK[:3] if 4 <= row <= 5 else BASE[:3] for row in range(40)]

    def read(offset: int):
        return [DARK[:3] if 4 <= row + offset - 2 <= 5 else BASE[:3] for row in range(40)]

    # 후보 순서는 0, +1, -1, +2, -2 … 이고, 다른 그림은 띠가 2칸 위에 있다.
    shifted = [read(0), read(1), read(-1), read(2), read(-2)]
    assert best_shift_index(reference, shifted) == 3
    # 무늬가 없으면 옮기지 않는다.
    flat = [BASE[:3]] * 40
    assert best_shift_index(flat, [flat, flat, flat]) == 0
    # 표본이 너무 적으면 옮기지 않는다.
    assert best_shift_index(reference[:5], [read(0)[:5], read(2)[:5]]) == 0


def test_solve_view_shifts_chains_back_through_sides() -> None:
    shifts = solve_view_shifts(
        {("FRONT", "RIGHT"): 0.02, ("FRONT", "LEFT"): 0.04, ("RIGHT", "BACK"): 0.01, ("LEFT", "BACK"): -0.01}
    )
    assert shifts == {"RIGHT": 0.02, "LEFT": 0.04, "BACK": 0.03}, shifts
    assert solve_view_shifts({("FRONT", "RIGHT"): 0.0}) == {}


def _striped(stripe_rows: range, size: int = 100):
    pixels = []
    for row in range(size):
        color = DARK if row in stripe_rows else BASE
        for _column in range(size):
            pixels.extend(color)
    return build_raster_source(
        width=size,
        height=size,
        pixels=pixels,
        subject_bbox=(0, 0, size, size),
        background=(1.0, 1.0, 1.0, 1.0),
        background_threshold=0.02,
        fallback_color=BASE,
    )


def test_rasterize_reads_side_view_at_matching_height() -> None:
    # 정면과 측면 사이 45도로 선 판. 두 시점 모두 이 판을 본다.
    # 실제 메시처럼 8×8칸으로 나눠 공유 표본이 충분하게 한다.
    corner = 0.7071067811865476
    cells = 8

    def vertex(i: int, j: int):
        s, t = i / cells, j / cells
        return (s - 0.5, s - 0.5, t - 0.5), (s, t)

    triangles = []
    for i in range(cells):
        for j in range(cells):
            corners = (vertex(i, j), vertex(i + 1, j), vertex(i + 1, j + 1), vertex(i, j + 1))
            for a, b, c in ((0, 1, 2), (0, 2, 3)):
                triangles.append(
                    BakeTriangle(
                        positions=(corners[a][0], corners[b][0], corners[c][0]),
                        uvs=(corners[a][1], corners[b][1], corners[c][1]),
                        normal=(corner, -corner, 0.0),
                        group=0,
                    )
                )
    # 측면 그림은 띠를 4% 높게 그렸다.
    sources = {
        "FRONT": _striped(range(40, 50)),
        "RIGHT": _striped(range(44, 54)),
        "BACK": _striped(range(0, 0)),
    }
    _plain, plain_metrics = rasterize_atlas(triangles, sources, 64, 0, (0.0, 0.0, 0.0), 1.2)
    assert plain_metrics["band_alignment"] == {}
    _aligned, metrics = rasterize_atlas(triangles, sources, 64, 0, (0.0, 0.0, 0.0), 1.2, align_bands=True)
    shift = metrics["band_alignment"].get("0:RIGHT")
    # 판 높이 1.0의 4%만큼 측면을 위로 옮겨 읽어야 정면과 맞는다.
    assert shift is not None and abs(shift - 0.04) <= 0.006, metrics["band_alignment"]


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"순수 띠 높이 맞춤 테스트 {len(tests)}/{len(tests)} 통과")
