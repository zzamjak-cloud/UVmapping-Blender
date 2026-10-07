"""시점마다 다른 높이에 그려진 벨트·띠를 같은 높이로 맞추는 수직 정렬.

투영 베이크는 정면·측면·뒷면 그림을 각자 높이 그대로 모델에 입히므로, AI가 시점마다
벨트를 1~3% 다른 높이에 그리면 모델에서 시점이 바뀌는 곳(옆구리)에 단차가 생긴다.

정면과 뒷면처럼 디자인이 다른 시점의 높이별 색 분포를 통째로 비교하면 서로 다른
무늬끼리 맞춰 버린다. 그래서 **두 시점이 함께 보는 같은 표면**(몸통 앞 모서리는 정면과
측면 모두에 그려진다)에서만 비교한다. 같은 표면 점의 색이 두 그림에서 같아지는 수직
어긋남을 이웃한 시점 쌍(정면-측면, 측면-뒷면)마다 찾고, 정면을 0으로 두고 이어 붙인다.
베이크는 그 시점·파츠의 표본을 그만큼 위아래로 옮겨 읽으므로, 색을 섞거나 흐리지 않고
그림 내용의 위치만 옮긴다.

이 모듈은 ``bpy`` 없이 동작해 순수 테스트로 검증한다.
"""

from __future__ import annotations

from typing import Mapping, Sequence


Color = Sequence[float]

# 찾아볼 수직 어긋남 후보: 모델 높이의 ±6%를 0.25% 간격으로.
BAND_SHIFT_STEP_RATIO = 0.0025
BAND_MAX_SHIFT_RATIO = 0.06
# 비교에 쓸 최소 공유 표본 수. 이보다 적으면 그 시점 쌍은 맞추지 않는다.
BAND_MIN_SAMPLES = 24
# 어긋남 0보다 오차가 이 비율 이상 줄어야 옮긴다. 무늬 없는 곳에서 우연히 옮겨지지 않게 한다.
_MIN_IMPROVEMENT = 0.2

# 이웃한 시점 쌍. 정면을 기준(0)으로 측면을 맞추고, 뒷면은 양 측면에서 이어 맞춘다.
VIEW_PAIRS = (("FRONT", "RIGHT"), ("FRONT", "LEFT"), ("RIGHT", "BACK"), ("LEFT", "BACK"))
REFERENCE_VIEW = "FRONT"


def shift_candidates(height_span: float) -> tuple[float, ...]:
    """시험할 수직 어긋남(월드 단위). 0이 첫 번째이고 가까운 값부터 나온다."""

    step = max(height_span * BAND_SHIFT_STEP_RATIO, 1.0e-6)
    count = max(1, int(round(BAND_MAX_SHIFT_RATIO / BAND_SHIFT_STEP_RATIO)))
    values = [0.0]
    for index in range(1, count + 1):
        values.extend((index * step, -index * step))
    return tuple(values)


def best_shift_index(reference: Sequence[Color], shifted: Sequence[Sequence[Color | None]]) -> int:
    """기준 색과 가장 잘 맞는 후보 번호. 0번 후보(어긋남 0)보다 충분히 낫지 않으면 0.

    Args:
        reference: 공유 표면 점마다 기준 시점 그림의 색
        shifted: 후보마다, 같은 점들을 다른 시점 그림에서 그 어긋남만큼 옮겨 읽은 색
            (읽지 못한 점은 None)
    """

    def error(colors: Sequence[Color | None]) -> tuple[float, int]:
        total = 0.0
        count = 0
        for expected, actual in zip(reference, colors):
            if actual is None:
                continue
            total += sum((expected[channel] - actual[channel]) ** 2 for channel in range(3))
            count += 1
        return (total / count if count else float("inf")), count

    if not shifted:
        return 0
    baseline, baseline_count = error(shifted[0])
    if baseline_count < BAND_MIN_SAMPLES:
        return 0
    best_index, best_error = 0, baseline
    for index in range(1, len(shifted)):
        value, count = error(shifted[index])
        if count >= max(BAND_MIN_SAMPLES, baseline_count // 2) and value < best_error:
            best_index, best_error = index, value
    if best_index and best_error > baseline * (1.0 - _MIN_IMPROVEMENT):
        return 0
    return best_index


def solve_view_shifts(pair_shifts: Mapping[tuple[str, str], float]) -> dict[str, float]:
    """이웃 시점 쌍의 상대 어긋남에서 정면 기준 시점별 어긋남을 구한다.

    ``pair_shifts[(A, B)] = d``는 B 그림을 d만큼 옮겨 읽어야 A와 맞는다는 뜻이다. 뒷면은
    두 측면 경로가 모두 있으면 평균, 하나만 있으면 그 값을 쓴다. 맞출 근거가 없는 시점은 0이다.
    """

    result = {REFERENCE_VIEW: 0.0}
    for side in ("RIGHT", "LEFT"):
        if (REFERENCE_VIEW, side) in pair_shifts:
            result[side] = pair_shifts[(REFERENCE_VIEW, side)]
    paths = [
        result[side] + pair_shifts[(side, "BACK")]
        for side in ("RIGHT", "LEFT")
        if side in result and (side, "BACK") in pair_shifts
    ]
    if paths:
        result["BACK"] = sum(paths) / len(paths)
    return {view: value for view, value in result.items() if view != REFERENCE_VIEW and value}


__all__ = (
    "BAND_MAX_SHIFT_RATIO",
    "BAND_MIN_SAMPLES",
    "BAND_SHIFT_STEP_RATIO",
    "REFERENCE_VIEW",
    "VIEW_PAIRS",
    "best_shift_index",
    "shift_candidates",
    "solve_view_shifts",
)
