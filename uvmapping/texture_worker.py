"""Blender 메인 프로세스 밖에서 OpenRouter HTTP 요청을 실행하는 작업자."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time
from urllib import error, request


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_pipeline import (  # noqa: E402
    validate_reference_image_path,
)
from uvmapping.openrouter_provider import (  # noqa: E402
    BASE_URL,
    CHAT_COMPLETIONS_ENDPOINT,
    IMAGES_ENDPOINT,
    build_analysis_payload,
    build_request_headers,
    build_turnaround_payload,
    extract_analysis_text,
    extract_image_response,
)


def _encode_images(paths: list[str]) -> tuple[tuple[str, str], ...]:
    encoded = []
    for raw_path in paths:
        path, mime_type = validate_reference_image_path(raw_path)
        encoded.append((mime_type, base64.b64encode(path.read_bytes()).decode("ascii")))
    return tuple(encoded)


# 재시도 정책. 그룹 여러 건을 동시에 호출하면 단일 호출보다 429를 만날 확률이 높다.
REQUEST_TIMEOUT_SECONDS = 300
MAX_RETRY_ATTEMPTS = 2  # 첫 시도 실패 후 추가로 시도하는 최대 횟수(총 3회 시도)
# 그룹 하나가 재시도에 쓸 수 있는 총 벽시계 시간. 요청 타임아웃이 300초라 횟수만으로는
# 최악의 대기가 15분을 넘고, 그동안 Blender 쪽은 결과를 기다리기만 한다.
MAX_RETRY_ELAPSED_SECONDS = 600.0
RETRY_BASE_DELAY_SECONDS = 2.0
RETRY_MAX_DELAY_SECONDS = 8.0
RETRY_JITTER_SECONDS = 0.25
RETRYABLE_STATUS_CODES = frozenset({429})  # 5xx는 범위로 따로 판정한다
MAX_PARALLEL_GROUP_REQUESTS = 4


def _sleep(seconds: float) -> None:
    """재시도 대기. 테스트가 모듈 속성으로 치환할 수 있게 함수로 분리한다."""

    time.sleep(seconds)


def _monotonic() -> float:
    """재시도 경과 측정. 테스트가 모듈 속성으로 치환할 수 있게 함수로 분리한다."""

    return time.monotonic()


def _urlopen(api_request: request.Request, timeout: int):
    """HTTP 호출 지점. 테스트가 모듈 속성으로 치환할 수 있게 함수로 분리한다."""

    return request.urlopen(api_request, timeout=timeout)


def _is_retryable_status(code: int) -> bool:
    """429와 5xx만 재시도한다. 나머지 4xx는 재시도해도 결과가 같다."""

    return code in RETRYABLE_STATUS_CODES or 500 <= code < 600


def _retry_delay_seconds(attempt_index: int) -> float:
    """지수 백오프 대기 시간(2·4·8초, 상한 적용)에 소량의 지터를 더한다."""

    delay = min(RETRY_BASE_DELAY_SECONDS * (2 ** attempt_index), RETRY_MAX_DELAY_SECONDS)
    return delay + random.uniform(0.0, RETRY_JITTER_SECONDS)


def _http_error_message(exc: error.HTTPError) -> str:
    detail = exc.read().decode("utf-8", errors="replace")
    try:
        message = json.loads(detail).get("error", {}).get("message", detail)
    except (json.JSONDecodeError, AttributeError):
        message = detail
    if exc.code in {401, 403}:
        message = f"{message} (OpenRouter API 키를 확인해 주세요)"
    return f"OpenRouter API 오류({exc.code}): {message}"


def _openrouter_request(endpoint: str, api_key: str, payload: dict) -> dict:
    """OpenRouter JSON 엔드포인트 하나를 호출하고 응답을 해석한다.

    429와 5xx는 지수 백오프로 최대 MAX_RETRY_ATTEMPTS회 재시도하되, 누적 경과가
    MAX_RETRY_ELAPSED_SECONDS를 넘길 재시도는 시작하지 않는다. 그 밖의 4xx는 즉시
    실패시킨다.
    """

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = build_request_headers(api_key)
    started = _monotonic()
    for attempt_index in range(MAX_RETRY_ATTEMPTS + 1):
        # 재시도마다 새 Request를 만든다. 소비된 요청 객체를 재사용하지 않기 위해서다.
        api_request = request.Request(
            f"{BASE_URL}/{endpoint}", data=body, method="POST", headers=headers
        )
        try:
            with _urlopen(api_request, REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            message = _http_error_message(exc)
            if attempt_index < MAX_RETRY_ATTEMPTS and _is_retryable_status(exc.code):
                delay = _retry_delay_seconds(attempt_index)
                if _monotonic() - started + delay <= MAX_RETRY_ELAPSED_SECONDS:
                    _sleep(delay)
                    continue
                raise RuntimeError(
                    f"{message} · 재시도 시간 상한 {MAX_RETRY_ELAPSED_SECONDS:.0f}초를 넘어 중단했습니다."
                ) from exc
            raise RuntimeError(message) from exc
        except error.URLError as exc:
            raise RuntimeError(f"OpenRouter API 연결 실패: {exc.reason}") from exc
    raise RuntimeError("OpenRouter API 재시도 횟수를 초과했습니다.")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        finally:
            raise


def _run_turnaround_group(group: dict, api_key: str) -> dict:
    """그룹(캔버스) 하나를 OpenRouter 1회 호출로 만들고 저장 경로만 돌려준다.

    단일 "turnaround" 작업과 배치의 그룹 하나가 완전히 같은 본문을 쓰도록 분리했다.
    작업 JSON에 실려 온 값만 사용하므로 호출 사이에 공유하는 상태가 없다.
    """

    # 종횡비·해상도·품질 등급은 레이아웃 계약과 모델 능력치에서 오므로
    # 작업 JSON에 실린 값만 그대로 전달한다. quality는 선택 필드다.
    options = {
        key: str(group[key])
        for key in ("aspect_ratio", "resolution", "quality")
        if group.get(key) is not None
    }
    payload = build_turnaround_payload(
        str(group["prompt"]),
        _encode_images(list(group.get("image_paths", ()))),
        model=str(group["model"]),
        **options,
    )
    response = _openrouter_request(IMAGES_ENDPOINT, api_key, payload)
    mime_type, image_data = extract_image_response(response)
    requested_path = Path(str(group["output_path"]))
    suffix = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime_type, ".png")
    output_path = requested_path.with_suffix(suffix)
    # 그룹마다 독립된 출력 경로에만 쓰므로 스레드끼리 같은 파일을 건드리지 않는다.
    _atomic_write(output_path, image_data)
    return {"ok": True, "output_path": str(output_path)}


def _group_name(group: dict, index: int) -> str:
    name = str(group.get("name", "")).strip()
    return name or f"GROUP_{index}"


def _run_turnaround_batch(groups: list, api_key: str) -> dict:
    """그룹 여러 건을 스레드풀로 동시에 호출하고 그룹별 성공·실패를 모두 보존한다.

    한 그룹이 실패해도 다른 그룹의 결과는 버리지 않는다. 배치 자체가 성립하면
    ok는 True이며, 그룹별 성공 여부로 무엇을 다시 요청할지는 호출 측이 정한다.
    """

    if not groups:
        raise ValueError("turnaround_batch 작업에 그룹이 하나도 없습니다.")

    results: list = [None] * len(groups)
    worker_count = min(MAX_PARALLEL_GROUP_REQUESTS, len(groups))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(_run_turnaround_group, group, api_key): index
            for index, group in enumerate(groups)
        }
        for future in futures:
            index = futures[future]
            name = _group_name(groups[index], index)
            try:
                outcome = future.result()
            except Exception as exc:  # 그룹 하나의 실패가 배치 전체를 죽이지 않는다.
                results[index] = {"name": name, "ok": False, "error": str(exc)}
            else:
                results[index] = {
                    "name": name,
                    "ok": True,
                    "output_path": outcome["output_path"],
                }

    failed = [entry["name"] for entry in results if not entry["ok"]]
    return {
        "ok": True,
        "groups": results,
        "summary": {
            "total": len(results),
            "succeeded": len(results) - len(failed),
            "failed": len(failed),
            "failed_groups": failed,
        },
    }


def run_job(job: dict, api_key: str) -> dict:
    """직렬화된 작업 하나를 실행하고 작은 결과 계약만 반환한다."""

    action = job.get("action")
    if action == "analyze":
        payload = build_analysis_payload(
            str(job["prompt"]),
            _encode_images(list(job.get("image_paths", ()))),
            model=str(job["model"]),
        )
        response = _openrouter_request(CHAT_COMPLETIONS_ENDPOINT, api_key, payload)
        text = extract_analysis_text(response)
        if not text.strip():
            raise RuntimeError("OpenRouter 응답에 참조 분석 텍스트가 없습니다.")
        return {"ok": True, "text": text}
    if action == "turnaround":
        return _run_turnaround_group(job, api_key)
    if action == "turnaround_batch":
        return _run_turnaround_batch(list(job.get("groups", ())), api_key)
    raise ValueError(f"지원하지 않는 작업입니다: {action!r}")


def main() -> None:
    separator = sys.argv.index("--")
    request_path = Path(sys.argv[separator + 1])
    response_path = Path(sys.argv[separator + 2])
    try:
        api_key = sys.stdin.readline().strip()
        if not api_key:
            raise ValueError("OpenRouter API 키가 작업자 프로세스에 전달되지 않았습니다.")
        job = json.loads(request_path.read_text(encoding="utf-8"))
        result = run_job(job, api_key)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    _atomic_write(
        response_path,
        json.dumps(result, ensure_ascii=False).encode("utf-8"),
    )


if __name__ == "__main__":
    main()
