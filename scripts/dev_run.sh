#!/bin/bash

set -euo pipefail

readonly UVMAPPING_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly UVMAPPING_REPOSITORY_ROOT="$(cd "${UVMAPPING_SCRIPT_DIR}/.." && pwd)"
readonly UVMAPPING_ADDON_ID="uvmapping_blender"
readonly UVMAPPING_PROFILE_NAME="UVmappingBlenderDev"
readonly UVMAPPING_MANIFEST="${UVMAPPING_REPOSITORY_ROOT}/blender_manifest.toml"
readonly UVMAPPING_BOOTSTRAP="${UVMAPPING_SCRIPT_DIR}/dev_bootstrap.py"
readonly UVMAPPING_BLENDER_EXECUTABLE="${UVMAPPING_BLENDER_BINARY:-/Applications/Blender.app/Contents/MacOS/Blender}"

if [[ ! -f "${UVMAPPING_MANIFEST}" ]]; then
  echo "오류: blender_manifest.toml을 찾을 수 없습니다: ${UVMAPPING_MANIFEST}" >&2
  exit 2
fi

if ! grep -Eq '^[[:space:]]*id[[:space:]]*=[[:space:]]*"uvmapping_blender"[[:space:]]*$' "${UVMAPPING_MANIFEST}"; then
  echo "오류: 매니페스트 id는 uvmapping_blender여야 합니다." >&2
  exit 2
fi

if [[ ! -x "${UVMAPPING_BLENDER_EXECUTABLE}" ]]; then
  echo "오류: Blender 실행 파일을 찾을 수 없습니다: ${UVMAPPING_BLENDER_EXECUTABLE}" >&2
  echo "UVMAPPING_BLENDER_BINARY로 경로를 지정할 수 있습니다." >&2
  exit 2
fi

UVMAPPING_DETECTED_VERSION="$("${UVMAPPING_BLENDER_EXECUTABLE}" --version | awk '/^Blender / { print $2; exit }')"
readonly UVMAPPING_PROFILE_VERSION="${UVMAPPING_BLENDER_VERSION:-${UVMAPPING_DETECTED_VERSION}}"
if [[ -z "${UVMAPPING_PROFILE_VERSION}" ]]; then
  echo "오류: Blender 버전을 확인할 수 없습니다." >&2
  exit 2
fi

readonly UVMAPPING_PROFILE_ROOT="${HOME}/Library/Application Support/Blender/${UVMAPPING_PROFILE_NAME}/${UVMAPPING_PROFILE_VERSION}"
readonly UVMAPPING_EXTENSION_ROOT="${UVMAPPING_PROFILE_ROOT}/extensions/user_default"
readonly UVMAPPING_ADDON_LINK="${UVMAPPING_EXTENSION_ROOT}/${UVMAPPING_ADDON_ID}"
readonly UVMAPPING_LINK_TEMP="${UVMAPPING_ADDON_LINK}.tmp.$$"

mkdir -p "${UVMAPPING_EXTENSION_ROOT}"

if [[ -e "${UVMAPPING_ADDON_LINK}" || -L "${UVMAPPING_ADDON_LINK}" ]]; then
  if [[ ! -L "${UVMAPPING_ADDON_LINK}" ]]; then
    echo "오류: 개발 Extension 위치에 링크가 아닌 항목이 있습니다: ${UVMAPPING_ADDON_LINK}" >&2
    exit 2
  fi
fi

cleanup_temporary_link() {
  if [[ -L "${UVMAPPING_LINK_TEMP}" ]]; then
    rm -f "${UVMAPPING_LINK_TEMP}"
  fi
}
trap cleanup_temporary_link EXIT

ln -s "${UVMAPPING_REPOSITORY_ROOT}" "${UVMAPPING_LINK_TEMP}"
mv -f -h "${UVMAPPING_LINK_TEMP}" "${UVMAPPING_ADDON_LINK}"

export BLENDER_USER_RESOURCES="${UVMAPPING_PROFILE_ROOT}"
export UVMAPPING_REPOSITORY_ROOT
export UVMAPPING_ADDON_ID
export UVMAPPING_PROFILE_ROOT

echo "개발 프로필: ${UVMAPPING_PROFILE_ROOT}"
echo "Extension 소스: ${UVMAPPING_ADDON_LINK} -> ${UVMAPPING_REPOSITORY_ROOT}"

"${UVMAPPING_BLENDER_EXECUTABLE}" \
  --background \
  --python-exit-code 1 \
  --python "${UVMAPPING_BOOTSTRAP}"

UVMAPPING_AUTOMATED=0
for UVMAPPING_ARGUMENT in "$@"; do
  case "${UVMAPPING_ARGUMENT}" in
    --background|--python|--python-expr)
      UVMAPPING_AUTOMATED=1
      ;;
  esac
done

if [[ "${UVMAPPING_AUTOMATED}" -eq 1 ]]; then
  exec "${UVMAPPING_BLENDER_EXECUTABLE}" --python-exit-code 1 "$@"
fi

exec "${UVMAPPING_BLENDER_EXECUTABLE}" "$@"
