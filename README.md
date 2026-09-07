# UVmapping Blender

참조 이미지와 한 줄 프롬프트로 AI 3면도를 생성하고, 이미 펼쳐 둔 UV에 손맵 텍스처를 베이크하는 Blender Extension입니다. UV 언랩은 Blender 기본 도구를 사용하고, 이 애드온은 텍스처링만 담당합니다.

## 요구 사항

- Blender 4.5 LTS 이상
- 편집 가능한 Mesh 객체와 **0-1 안에 배치된 UV 맵**
- Gemini 또는 OpenAI API 키와 Blender의 Online Access 허용

## 설치

1. Blender에서 `Edit > Preferences > Get Extensions`를 엽니다.
2. 오른쪽 위 메뉴에서 `Repositories > Add Remote Repository`를 선택합니다.
3. 저장소 URL에 다음 주소를 입력합니다.

   ```text
   https://zzamjak-cloud.github.io/UVmapping-Blender/index.json
   ```

4. `Refresh Remote`를 실행한 뒤 `UV Mapping Blender`를 검색합니다.
5. `Install`을 눌러 설치하고 Extension을 활성화합니다.

### 업데이트 확인

Blender의 Extension 저장소를 새로 고치면 사용 가능한 업데이트를 확인할 수 있습니다. `Check for Updates on Startup`을 켜면 Blender를 시작할 때 새 버전을 확인해 알려줍니다. 업데이트는 사용자가 승인해야 하며 무인으로 자동 설치되지 않습니다.

## 사용법

### 준비: UV 펼치기

이 애드온은 UV를 만들지 않습니다. Blender 기본 도구로 UV를 먼저 준비합니다.

1. `UV Editing` 워크스페이스에서 Seam을 표시하고 `U > Unwrap`(또는 `Smart UV Project`)으로 UV를 만듭니다.
2. `UV > Pack Islands`로 모든 UV를 0-1 안에 배치합니다.
3. 여러 객체를 한 장의 텍스처에 함께 구울 때는 객체들의 UV가 **서로 겹치지 않아야** 합니다. 겹치면 3면도 생성 단계에서 막고 안내합니다.
4. UV가 없거나 0-1을 벗어나면 패널에 경고가 표시되고 생성이 중단됩니다.

### AI 손맵 텍스처 디자인

1. Object Mode에서 텍스처를 만들 Mesh 객체를 모두 선택하고 `3D Viewport > Sidebar(N) > UV Mapping`에서 `AI 손맵 텍스처` 패널을 엽니다.
2. Pinterest 등에서 저장한 JPG, PNG 또는 WebP 참조 이미지를 최대 5장 추가합니다. macOS와 Windows에서는 브라우저의 `이미지 복사` 후 `클립보드` 버튼으로 바로 붙여넣을 수도 있습니다.
3. `Edit > Preferences > Add-ons > UV Mapping Blender`에서 사용할 Provider의 개인 API 키를 입력합니다. 키는 프로젝트나 `.blend`가 아닌 Blender 개인 환경설정에 저장되고 입력창에서는 가려지지만, OS Keychain 암호화 저장소는 아닙니다. 공유 PC에서는 환경 변수 `GEMINI_API_KEY` 또는 `OPENAI_API_KEY` 사용을 권장합니다.
4. 패널 상단의 `이미지 생성 모델`에서 `Nano Banana Pro` 또는 `GPT-Image-2 (덕테이프)` 버튼을 선택하고 `참조 이미지 분석`을 누릅니다. 선택한 모델에 맞춰 분석 Provider와 3면도 생성 모델이 함께 전환됩니다.
5. 참조 이미지에 없는 요구만 `추가 지시`에 한 문장으로 입력합니다. Blender의 한글 조합이 불안정하면 `한글 프롬프트 입력` 버튼을 눌러 OS 입력 창에서 작성합니다. 완료하면 한 줄로 정리되어 반영되고, 취소하면 기존 내용이 유지됩니다. 참조 이미지 없이 프롬프트만으로도 생성할 수 있습니다.
6. `출력 텍스처`에서 베이크할 `텍스처 크기`와 `UV 패딩`을 정합니다.
7. `단일 3면도 생성`을 누르면 선택 모델을 FRONT, RIGHT, BACK 방향으로 로컬 캡처한 뒤 Nano Banana Pro 또는 GPT-Image-2를 한 번만 호출합니다. GPT-Image-2는 2352×1008 해상도와 결과 한 장으로 고정됩니다.
8. 결과는 세 방향이 같은 폭으로 들어간 이미지 한 장과 로컬에서 분리한 세 이미지로 저장됩니다. 저장된 `.blend`는 옆의 `textures` 폴더, 저장하지 않은 파일은 임시 폴더를 사용합니다.
9. `Diffuse/Albedo 적용`을 누르면 FRONT, RIGHT, BACK과 좌우 반전한 측면을 모델 표면에 깊이 인식 투영하고 현재 UV Atlas로 베이크합니다. 이 단계는 로컬 처리이므로 AI를 다시 호출하거나 추가 비용을 사용하지 않습니다.
10. 완성된 PNG와 sRGB Image Texture가 연결된 Principled BSDF 머티리얼이 선택 Mesh에 적용됩니다. 원본 머티리얼 슬롯은 삭제하지 않습니다.

현재 CPU 베이크는 최대 4096×4096을 지원하며 기본 2048×2048을 권장합니다. 3면도에 직접 보이지 않는 상·하면은 인접 시점의 색을 확장해 채우므로, 의미 있는 상면 그림이 꼭 필요한 에셋은 추후 상면 참조 또는 별도 보정을 권장합니다. 3면도 생성 시점과 베이크 시점에 각각 현재 UV로 계약을 다시 계산해 비교하므로, 모델 Transform, 메시 또는 UV가 그 사이에 바뀌면 잘못 투영하지 않고 재생성을 안내합니다.

## 릴리스 운영

`v*` 태그를 push하면 전체 회귀 검사와 공식 Blender Extension 빌드를 거쳐 GitHub Release 초안과 ZIP 자산까지만 생성합니다. 초안은 자동으로 공개하지 않습니다. 자산의 크기, SHA-256, 태그 대상 커밋을 `distribution/releases.lock.json`에 반영해 기본 브랜치에 push한 뒤 초안을 공개해야 합니다.

공개 이벤트는 보호된 Pages 환경에서 태그를 직접 배포하지 않고 기본 브랜치의 `workflow_dispatch`를 호출합니다. 배포 작업은 잠금 파일에 기록된 모든 공개 Release와 자산을 검증하지만, Blender 원격 저장소에는 SemVer상 최신 ZIP과 인덱스 항목 하나만 게시합니다. 따라서 과거 Release는 검증 가능한 이력으로 유지하면서도 동일 Extension ID의 중복 항목 때문에 업데이트가 이전 버전에 머무는 문제를 방지합니다.

## 라이선스

GPL-3.0-or-later. 자세한 내용은 [LICENSE](LICENSE)를 참조하세요.
