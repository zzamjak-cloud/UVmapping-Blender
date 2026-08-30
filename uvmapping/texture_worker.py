"""Blender 메인 프로세스 밖에서 이미지 AI HTTP 요청을 실행하는 작업자."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib import error, parse, request


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmapping.texture_pipeline import (  # noqa: E402
    build_gemini_analysis_payload,
    build_gemini_turnaround_payload,
    extract_gemini_generate_content_response,
    extract_gemini_text,
    validate_reference_image_path,
)
from uvmapping.openai_provider import (  # noqa: E402
    build_openai_analysis_payload,
    build_openai_image_edit_multipart,
    extract_openai_image_response,
    extract_openai_response_text,
)


def _encode_images(paths: list[str]) -> tuple[tuple[str, str], ...]:
    encoded = []
    for raw_path in paths:
        path, mime_type = validate_reference_image_path(raw_path)
        encoded.append((mime_type, base64.b64encode(path.read_bytes()).decode("ascii")))
    return tuple(encoded)


def _binary_images(paths: list[str]) -> tuple[tuple[str, str, bytes], ...]:
    images = []
    for index, raw_path in enumerate(paths):
        path, mime_type = validate_reference_image_path(raw_path)
        filename = f"{index:02d}_{path.name}"
        images.append((filename, mime_type, path.read_bytes()))
    return tuple(images)


def _gemini_request(model: str, api_key: str, payload: dict) -> dict:
    safe_model = parse.quote(model.strip(), safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{safe_model}:generateContent"
    )
    api_request = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with request.urlopen(api_request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            message = detail
        raise RuntimeError(f"Gemini API 오류({exc.code}): {message}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Gemini API 연결 실패: {exc.reason}") from exc


def _openai_request(
    endpoint: str,
    api_key: str,
    body: bytes,
    content_type: str,
) -> dict:
    api_request = request.Request(
        f"https://api.openai.com/v1/{endpoint}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
        },
    )
    try:
        with request.urlopen(api_request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            message = detail
        raise RuntimeError(f"OpenAI API 오류({exc.code}): {message}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"OpenAI API 연결 실패: {exc.reason}") from exc


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
    provider = str(job.get("provider", "GEMINI")).upper()
    image_paths = list(job.get("image_paths", ()))
    if action == "analyze":
        images = _encode_images(image_paths)
        if provider == "OPENAI":
            payload = build_openai_analysis_payload(
                str(job["prompt"]), images, model=str(job["model"])
            )
            response = _openai_request(
                "responses",
                api_key,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json",
            )
            text = extract_openai_response_text(response)
        elif provider == "GEMINI":
            payload = build_gemini_analysis_payload(str(job["prompt"]), images)
            response = _gemini_request(str(job["model"]), api_key, payload)
            text = extract_gemini_text(response)
        else:
            raise ValueError(f"지원하지 않는 AI Provider입니다: {provider!r}")
        if not text.strip():
            raise RuntimeError(f"{provider} 응답에 참조 분석 텍스트가 없습니다.")
        return {"ok": True, "text": text}
    if action == "turnaround":
        if provider == "OPENAI":
            content_type, body = build_openai_image_edit_multipart(
                str(job["prompt"]),
                _binary_images(image_paths),
                model=str(job["model"]),
            )
            response = _openai_request("images/edits", api_key, body, content_type)
            mime_type, image_data = extract_openai_image_response(response)
        elif provider == "GEMINI":
            images = _encode_images(image_paths)
            payload = build_gemini_turnaround_payload(str(job["prompt"]), images)
            response = _gemini_request(str(job["model"]), api_key, payload)
            generated = extract_gemini_generate_content_response(response).images
            if len(generated) != 1:
                raise RuntimeError(
                    f"Gemini 결과 이미지가 정확히 한 장이어야 합니다: {len(generated)}장"
                )
            mime_type = generated[0].mime_type
            image_data_base64 = generated[0].data_base64
            if mime_type not in {"image/png", "image/jpeg"}:
                raise RuntimeError(f"지원하지 않는 Gemini 이미지 형식입니다: {mime_type}")
            image_data = base64.b64decode(image_data_base64, validate=True)
        else:
            raise ValueError(f"지원하지 않는 AI Provider입니다: {provider!r}")
        requested_path = Path(str(job["output_path"]))
        suffix = ".jpg" if mime_type == "image/jpeg" else ".png"
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
            raise ValueError("AI API 키가 작업자 프로세스에 전달되지 않았습니다.")
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
