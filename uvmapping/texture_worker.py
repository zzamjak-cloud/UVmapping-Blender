"""Blender 메인 프로세스 밖에서 OpenRouter HTTP 요청을 실행하는 작업자."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
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


def _openrouter_request(endpoint: str, api_key: str, payload: dict) -> dict:
    """OpenRouter JSON 엔드포인트 하나를 호출하고 응답을 해석한다."""

    api_request = request.Request(
        f"{BASE_URL}/{endpoint}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=build_request_headers(api_key),
    )
    try:
        with request.urlopen(api_request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except (json.JSONDecodeError, AttributeError):
            message = detail
        if exc.code in {401, 403}:
            message = f"{message} (OpenRouter API 키를 확인해 주세요)"
        raise RuntimeError(f"OpenRouter API 오류({exc.code}): {message}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"OpenRouter API 연결 실패: {exc.reason}") from exc


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


def run_job(job: dict, api_key: str) -> dict:
    """직렬화된 작업 하나를 실행하고 작은 결과 계약만 반환한다."""

    action = job.get("action")
    image_paths = list(job.get("image_paths", ()))
    if action == "analyze":
        payload = build_analysis_payload(
            str(job["prompt"]), _encode_images(image_paths), model=str(job["model"])
        )
        response = _openrouter_request(CHAT_COMPLETIONS_ENDPOINT, api_key, payload)
        text = extract_analysis_text(response)
        if not text.strip():
            raise RuntimeError("OpenRouter 응답에 참조 분석 텍스트가 없습니다.")
        return {"ok": True, "text": text}
    if action == "turnaround":
        payload = build_turnaround_payload(
            str(job["prompt"]), _encode_images(image_paths), model=str(job["model"])
        )
        response = _openrouter_request(IMAGES_ENDPOINT, api_key, payload)
        mime_type, image_data = extract_image_response(response)
        requested_path = Path(str(job["output_path"]))
        suffix = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime_type, ".png")
        output_path = requested_path.with_suffix(suffix)
        _atomic_write(output_path, image_data)
        return {"ok": True, "output_path": str(output_path)}
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
