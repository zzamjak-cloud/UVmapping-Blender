"""텍스처 작업자의 Blender 비의존 회귀 테스트.

다면도 그룹을 여러 건 병렬로 호출하는 "turnaround_batch" 액션과 429·5xx 재시도를
검사한다. 기존 "turnaround" 단일 액션의 요청·응답 계약이 리팩터 후에도 그대로인지도
같이 확인한다. HTTP는 모두 모듈 속성 치환으로 대체하므로 네트워크를 쓰지 않는다.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import threading


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urllib import error  # noqa: E402

from uvmapping import texture_worker  # noqa: E402
from uvmapping.openrouter_provider import (  # noqa: E402
    DEFAULT_IMAGE_MODEL,
    IMAGES_ENDPOINT,
)


_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake\x00\x00\x00\x00IEND\xaeB`\x82"
_JPG_BYTES = b"\xff\xd8\xfffake\xff\xd9"


def _expect_error(callable_, fragment: str, kind=Exception) -> str:
    try:
        callable_()
    except kind as exc:
        assert fragment in str(exc), f"예상 문구 {fragment!r}가 없습니다: {exc}"
        return str(exc)
    raise AssertionError(f"{kind.__name__}가 발생하지 않았습니다: {fragment!r}")


def _image_response(data: bytes = _PNG_BYTES, media_type: str = "image/png") -> dict:
    import base64

    return {
        "data": [
            {
                "b64_json": base64.b64encode(data).decode("ascii"),
                "media_type": media_type,
            }
        ]
    }


class _PatchedWorker:
    """texture_worker의 모듈 속성을 임시로 치환하는 컨텍스트 매니저."""

    def __init__(self, **attributes) -> None:
        self._attributes = attributes
        self._originals: dict = {}

    def __enter__(self) -> None:
        for name, value in self._attributes.items():
            self._originals[name] = getattr(texture_worker, name)
            setattr(texture_worker, name, value)

    def __exit__(self, *exc_info) -> bool:
        for name, value in self._originals.items():
            setattr(texture_worker, name, value)
        return False


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _http_error(code: int, message: str = "요청 실패") -> error.HTTPError:
    body = json.dumps({"error": {"message": message}}, ensure_ascii=False).encode("utf-8")
    return error.HTTPError(
        "https://openrouter.ai/api/v1/images", code, message, {}, io.BytesIO(body)
    )


def _write_sheet(directory: Path, name: str = "sheet.png") -> Path:
    sheet = directory / name
    sheet.write_bytes(_PNG_BYTES)
    return sheet


def _group_job(sheet: Path, output: Path, name: str, **overrides) -> dict:
    group = {
        "name": name,
        "model": DEFAULT_IMAGE_MODEL,
        "prompt": f"{name} 캔버스를 그리세요",
        "image_paths": [str(sheet)],
        "output_path": str(output),
        "aspect_ratio": "1:1",
        "resolution": "2K",
    }
    group.update(overrides)
    return group


# --- 배치 디스패치 ------------------------------------------------------------


def test_batch_runs_every_group_in_parallel_and_keeps_request_order() -> None:
    """그룹 4건이 실제로 동시에 떠야 하고, 결과는 요청 순서와 이름으로 매핑된다."""

    barrier = threading.Barrier(4, timeout=10)
    captured: dict = {}
    lock = threading.Lock()

    def fake_request(endpoint, api_key, payload):
        # 4건이 모두 들어와야 배리어가 풀린다. 직렬 실행이면 여기서 시간 초과로 실패한다.
        barrier.wait()
        with lock:
            captured[payload["prompt"]] = (endpoint, payload["aspect_ratio"], payload["resolution"])
        return _image_response()

    with tempfile.TemporaryDirectory(prefix="uvmapping-worker-batch-") as temp_dir:
        root = Path(temp_dir)
        sheet = _write_sheet(root)
        names = ("QUAD_FRONT", "QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS")
        groups = [
            _group_job(
                sheet,
                root / f"{name.lower()}.png",
                name,
                aspect_ratio="1:1" if name in {"QUAD_FRONT", "QUAD_BACK"} else "16:9",
            )
            for name in names
        ]
        with _PatchedWorker(_openrouter_request=fake_request):
            result = texture_worker.run_job(
                {"action": "turnaround_batch", "groups": groups}, "sk-or-v1-test"
            )

        assert result["ok"] is True
        assert [entry["name"] for entry in result["groups"]] == list(names)
        assert all(entry["ok"] is True for entry in result["groups"])
        assert result["summary"] == {
            "total": 4,
            "succeeded": 4,
            "failed": 0,
            "failed_groups": [],
        }
        for entry in result["groups"]:
            assert Path(entry["output_path"]).is_file()
            assert "error" not in entry
        # 그룹마다 독립된 출력 파일을 쓴다.
        assert len({entry["output_path"] for entry in result["groups"]}) == 4

        # 그룹별 종횡비·해상도·프롬프트가 각자의 페이로드로 따로 전달된다.
        assert len(captured) == 4
        assert captured["QUAD_FRONT 캔버스를 그리세요"] == (IMAGES_ENDPOINT, "1:1", "2K")
        assert captured["QUAD_SIDES 캔버스를 그리세요"] == (IMAGES_ENDPOINT, "16:9", "2K")

        # 응답 전체가 그대로 JSON 직렬화되어야 한다(작업자는 파일로 응답을 남긴다).
        json.dumps(result, ensure_ascii=False)


def test_batch_preserves_successful_groups_when_one_group_fails() -> None:
    """한 그룹이 예외를 던져도 나머지 그룹의 결과 경로는 살아 돌아온다."""

    def fake_request(endpoint, api_key, payload):
        if "QUAD_SIDES" in payload["prompt"]:
            raise RuntimeError("OpenRouter API 오류(429): rate limit")
        return _image_response()

    with tempfile.TemporaryDirectory(prefix="uvmapping-worker-partial-") as temp_dir:
        root = Path(temp_dir)
        sheet = _write_sheet(root)
        names = ("QUAD_FRONT", "QUAD_BACK", "QUAD_SIDES", "QUAD_CAPS")
        groups = [_group_job(sheet, root / f"{name.lower()}.png", name) for name in names]
        with _PatchedWorker(_openrouter_request=fake_request):
            result = texture_worker.run_job(
                {"action": "turnaround_batch", "groups": groups}, "sk-or-v1-test"
            )

        # 배치 자체는 성립했으므로 ok는 True다. 판단은 호출 측이 한다.
        assert result["ok"] is True
        by_name = {entry["name"]: entry for entry in result["groups"]}
        assert by_name["QUAD_SIDES"]["ok"] is False
        assert "429" in by_name["QUAD_SIDES"]["error"]
        assert "output_path" not in by_name["QUAD_SIDES"]
        for name in ("QUAD_FRONT", "QUAD_BACK", "QUAD_CAPS"):
            assert by_name[name]["ok"] is True
            assert Path(by_name[name]["output_path"]).is_file()
        assert result["summary"] == {
            "total": 4,
            "succeeded": 3,
            "failed": 1,
            "failed_groups": ["QUAD_SIDES"],
        }


def test_batch_rejects_empty_group_list() -> None:
    _expect_error(
        lambda: texture_worker.run_job(
            {"action": "turnaround_batch", "groups": []}, "sk-or-v1-test"
        ),
        "그룹이 하나도 없습니다",
        ValueError,
    )


def test_unsupported_action_still_fails_with_korean_message() -> None:
    _expect_error(
        lambda: texture_worker.run_job({"action": "turnaround_quad"}, "sk-or-v1-test"),
        "지원하지 않는 작업",
        ValueError,
    )


# --- 재시도 정책 --------------------------------------------------------------


def _record_sleep(delays: list):
    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    return fake_sleep


def test_retry_recovers_after_two_rate_limit_responses() -> None:
    """429는 지수 백오프로 다시 시도하고, 성공하면 그 응답을 그대로 돌려준다."""

    attempts: list = []
    delays: list = []

    def fake_urlopen(api_request, timeout):
        attempts.append(timeout)
        if len(attempts) <= 2:
            raise _http_error(429, "rate limit")
        return _FakeHTTPResponse({"data": [{"b64_json": "ok"}]})

    with _PatchedWorker(_urlopen=fake_urlopen, _sleep=_record_sleep(delays)):
        response = texture_worker._openrouter_request(
            IMAGES_ENDPOINT, "sk-or-v1-test", {"prompt": "p"}
        )

    assert response == {"data": [{"b64_json": "ok"}]}
    assert len(attempts) == 3 and set(attempts) == {texture_worker.REQUEST_TIMEOUT_SECONDS}
    # 대기 시간은 2초·4초에 소량의 지터가 붙는다.
    assert len(delays) == 2
    assert 2.0 <= delays[0] < 2.0 + texture_worker.RETRY_JITTER_SECONDS + 1e-9
    assert 4.0 <= delays[1] < 4.0 + texture_worker.RETRY_JITTER_SECONDS + 1e-9


def test_retry_gives_up_after_max_attempts_on_server_error() -> None:
    """5xx가 계속되면 최대 재시도 횟수까지만 시도하고 마지막 오류를 올린다."""

    attempts: list = []
    delays: list = []

    def fake_urlopen(api_request, timeout):
        attempts.append(503)
        raise _http_error(503, "service unavailable")

    with _PatchedWorker(_urlopen=fake_urlopen, _sleep=_record_sleep(delays)):
        message = _expect_error(
            lambda: texture_worker._openrouter_request(
                IMAGES_ENDPOINT, "sk-or-v1-test", {"prompt": "p"}
            ),
            "OpenRouter API 오류(503)",
            RuntimeError,
        )

    assert "service unavailable" in message
    assert len(attempts) == texture_worker.MAX_RETRY_ATTEMPTS + 1
    assert len(delays) == texture_worker.MAX_RETRY_ATTEMPTS
    # 상한을 넘지 않는다.
    assert max(delays) <= texture_worker.RETRY_MAX_DELAY_SECONDS + texture_worker.RETRY_JITTER_SECONDS


def test_retry_stops_at_elapsed_time_limit() -> None:
    """요청이 타임아웃까지 끌면 남은 재시도 횟수가 있어도 시간 상한에서 멈춘다."""

    attempts: list = []
    delays: list = []
    clock = [0.0]

    def fake_urlopen(api_request, timeout):
        # 각 시도가 요청 타임아웃을 모두 소진한 최악의 경우를 흉내 낸다.
        attempts.append(timeout)
        clock[0] += texture_worker.REQUEST_TIMEOUT_SECONDS
        raise _http_error(503, "service unavailable")

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        clock[0] += seconds

    with _PatchedWorker(
        _urlopen=fake_urlopen,
        _sleep=fake_sleep,
        _monotonic=lambda: clock[0],
    ):
        message = _expect_error(
            lambda: texture_worker._openrouter_request(
                IMAGES_ENDPOINT, "sk-or-v1-test", {"prompt": "p"}
            ),
            "재시도 시간 상한",
            RuntimeError,
        )

    assert "service unavailable" in message
    # 횟수 상한(3회 시도)보다 시간 상한이 먼저 걸려 시도가 줄어든다.
    assert len(attempts) < texture_worker.MAX_RETRY_ATTEMPTS + 1, attempts
    # 진행 중인 요청은 끊을 수 없으므로 초과분은 요청 타임아웃 한 번을 넘지 않는다.
    assert clock[0] <= (
        texture_worker.MAX_RETRY_ELAPSED_SECONDS + texture_worker.REQUEST_TIMEOUT_SECONDS
    ), clock[0]
    assert len(delays) == len(attempts) - 1, (delays, attempts)


def test_retry_elapsed_limit_covers_worst_case_wall_clock() -> None:
    """정책 상수만으로도 그룹당 최악 대기가 상한 안에 들어와야 한다."""

    worst_case = texture_worker.MAX_RETRY_ELAPSED_SECONDS + texture_worker.REQUEST_TIMEOUT_SECONDS
    naive = texture_worker.REQUEST_TIMEOUT_SECONDS * (texture_worker.MAX_RETRY_ATTEMPTS + 1) + sum(
        texture_worker.RETRY_MAX_DELAY_SECONDS for _ in range(texture_worker.MAX_RETRY_ATTEMPTS)
    )
    assert worst_case <= naive + 1e-9, (worst_case, naive)
    assert texture_worker.MAX_RETRY_ATTEMPTS <= 2, texture_worker.MAX_RETRY_ATTEMPTS


def test_client_errors_other_than_rate_limit_fail_immediately() -> None:
    """429가 아닌 4xx는 재시도해도 결과가 같으므로 한 번만 시도한다."""

    for code, fragment in ((400, "OpenRouter API 오류(400)"), (401, "OpenRouter API 키")):
        attempts: list = []
        delays: list = []

        def fake_urlopen(api_request, timeout, code=code):
            attempts.append(code)
            raise _http_error(code, "잘못된 요청")

        with _PatchedWorker(_urlopen=fake_urlopen, _sleep=_record_sleep(delays)):
            _expect_error(
                lambda: texture_worker._openrouter_request(
                    IMAGES_ENDPOINT, "sk-or-v1-test", {"prompt": "p"}
                ),
                fragment,
                RuntimeError,
            )
        assert attempts == [code], f"{code}은(는) 재시도하면 안 됩니다."
        assert delays == []


def test_connection_failures_are_not_retried() -> None:
    """연결 실패는 상태 코드가 없으므로 기존처럼 즉시 오류로 보고한다."""

    attempts: list = []

    def fake_urlopen(api_request, timeout):
        attempts.append(1)
        raise error.URLError("연결 거부")

    with _PatchedWorker(_urlopen=fake_urlopen, _sleep=_record_sleep([])):
        _expect_error(
            lambda: texture_worker._openrouter_request(
                IMAGES_ENDPOINT, "sk-or-v1-test", {"prompt": "p"}
            ),
            "OpenRouter API 연결 실패",
            RuntimeError,
        )
    assert attempts == [1]


def test_retry_status_classification_covers_429_and_5xx_only() -> None:
    for code in (429, 500, 502, 503, 504, 599):
        assert texture_worker._is_retryable_status(code) is True, code
    for code in (400, 401, 403, 404, 409, 422):
        assert texture_worker._is_retryable_status(code) is False, code


# --- 기존 단일 액션 회귀 ------------------------------------------------------


def test_optional_quality_field_reaches_the_request_body() -> None:
    """작업 JSON의 선택 필드 quality가 단일·배치 모두에서 본문까지 전달된다."""

    captured: list = []

    def fake_request(endpoint, api_key, payload):
        captured.append(payload)
        return _image_response()

    gpt_model = "openai/gpt-image-2.5-sunburst"
    with tempfile.TemporaryDirectory(prefix="uvmapping-worker-quality-") as temp_dir:
        root = Path(temp_dir)
        sheet = _write_sheet(root)
        job = {
            "action": "turnaround",
            "model": gpt_model,
            "prompt": "품질 지정",
            "image_paths": [str(sheet)],
            "output_path": str(root / "single.png"),
            "aspect_ratio": "16:9",
            "resolution": "1K",
            "quality": "max",
        }
        with _PatchedWorker(_openrouter_request=fake_request):
            assert texture_worker.run_job(job, "sk-or-v1-test")["ok"] is True

            # quality가 없으면 이미지 크기 매핑으로 돌아간다.
            without_quality = {k: v for k, v in job.items() if k != "quality"}
            without_quality["output_path"] = str(root / "single-default.png")
            assert texture_worker.run_job(without_quality, "sk-or-v1-test")["ok"] is True

            # 배치 그룹도 같은 경로를 쓴다.
            batch = {
                "action": "turnaround_batch",
                "groups": [
                    _group_job(
                        sheet,
                        root / "group.png",
                        "QUAD_FRONT",
                        model=gpt_model,
                        quality="low",
                    )
                ],
            }
            assert texture_worker.run_job(batch, "sk-or-v1-test")["ok"] is True

    assert [payload.get("quality") for payload in captured] == ["max", "medium", "low"]
    # resolution을 받지 않는 모델이므로 어느 경로에서도 키가 실리지 않는다.
    assert all("resolution" not in payload for payload in captured)


def test_single_turnaround_action_keeps_its_request_and_response_contract() -> None:
    """리팩터 후에도 "turnaround" 액션의 요청 본문과 응답 키가 그대로여야 한다."""

    captured: list = []

    def fake_request(endpoint, api_key, payload):
        captured.append((endpoint, api_key, payload))
        return _image_response(_JPG_BYTES, "image/jpeg")

    with tempfile.TemporaryDirectory(prefix="uvmapping-worker-single-") as temp_dir:
        root = Path(temp_dir)
        sheet = _write_sheet(root)
        output = root / "turnaround.png"
        job = {
            "action": "turnaround",
            "model": DEFAULT_IMAGE_MODEL,
            "prompt": "3면도를 그리세요",
            "image_paths": [str(sheet)],
            "output_path": str(output),
            "aspect_ratio": "21:9",
            "resolution": "1K",
        }
        with _PatchedWorker(_openrouter_request=fake_request):
            result = texture_worker.run_job(job, "sk-or-v1-test")

        # 응답 키 집합은 2.3.0과 동일하다.
        assert set(result) == {"ok", "output_path"}
        assert result["ok"] is True
        # 결과 MIME에 맞춰 확장자를 바꾸는 규칙도 그대로다.
        assert result["output_path"] == str(root / "turnaround.jpg")
        assert Path(result["output_path"]).read_bytes() == _JPG_BYTES

        endpoint, api_key, payload = captured[-1]
        assert endpoint == IMAGES_ENDPOINT
        assert api_key == "sk-or-v1-test"
        assert payload["prompt"] == "3면도를 그리세요"
        assert payload["model"] == DEFAULT_IMAGE_MODEL
        assert payload["n"] == 1
        assert (payload["aspect_ratio"], payload["resolution"]) == ("21:9", "1K")
        assert len(payload["input_references"]) == 1


def test_single_turnaround_and_batch_group_share_one_code_path() -> None:
    """단일 액션과 배치 그룹이 같은 함수를 쓰므로 페이로드가 동일해야 한다."""

    captured: list = []

    def fake_request(endpoint, api_key, payload):
        captured.append(payload)
        return _image_response()

    with tempfile.TemporaryDirectory(prefix="uvmapping-worker-shared-") as temp_dir:
        root = Path(temp_dir)
        sheet = _write_sheet(root)
        single = {
            "action": "turnaround",
            "model": DEFAULT_IMAGE_MODEL,
            "prompt": "같은 프롬프트",
            "image_paths": [str(sheet)],
            "output_path": str(root / "single.png"),
            "aspect_ratio": "1:1",
            "resolution": "2K",
        }
        batch_group = dict(single)
        batch_group.pop("action")
        batch_group["name"] = "QUAD_FRONT"
        batch_group["output_path"] = str(root / "group.png")

        with _PatchedWorker(_openrouter_request=fake_request):
            texture_worker.run_job(single, "sk-or-v1-test")
            texture_worker.run_job(
                {"action": "turnaround_batch", "groups": [batch_group]}, "sk-or-v1-test"
            )

    assert len(captured) == 2
    assert captured[0] == captured[1]


def test_batch_group_without_name_falls_back_to_index_label() -> None:
    def fake_request(endpoint, api_key, payload):
        return _image_response()

    with tempfile.TemporaryDirectory(prefix="uvmapping-worker-noname-") as temp_dir:
        root = Path(temp_dir)
        sheet = _write_sheet(root)
        group = _group_job(sheet, root / "out.png", "QUAD_FRONT")
        group.pop("name")
        with _PatchedWorker(_openrouter_request=fake_request):
            result = texture_worker.run_job(
                {"action": "turnaround_batch", "groups": [group]}, "sk-or-v1-test"
            )
    assert result["groups"][0]["name"] == "GROUP_0"


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for name, test in tests:
        test()
        print(f"PASS {name}")
    print(f"텍스처 작업자 순수 테스트 {len(tests)}/{len(tests)} 통과")
