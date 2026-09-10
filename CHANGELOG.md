# 변경 기록

이 프로젝트는 [Semantic Versioning](https://semver.org/)을 따릅니다.

## 2.2.0 - 2026-09-10

### 수정 (형상 정확도)

- 참조 이미지를 넣으면 이미지 모델이 **참조의 캐릭터를 그대로 재생성**해 모델 실루엣과 비율을 무시하고, 그 결과 헬멧이 얼굴에·얼굴이 가슴에 찍히는 식으로 텍스처가 완전히 어긋나던 문제를 해결했다.
  - 3면도 생성 호출에는 기본적으로 **모델 형상 contact sheet 한 장만** 보내고, 스타일은 분석 결과(텍스트)로 전달한다. 원본 참조를 함께 보내려면 `생성에도 참조 이미지 전달`을 켠다(형상 복제 위험 경고 표시).
  - 프롬프트에 다른 모든 지시보다 우선하는 **형상 계약**을 넣었다. 실루엣·비율·자세·팔다리 각도·머리 비율 유지, 부품 추가·삭제 금지, 충돌 시 언제나 형상 이미지 우선.
  - 참조를 함께 보낼 때 그 역할을 `palette_only`로 못 박아 색과 재질 표현만 쓰도록 했다.
- 모델 형상 캡처 해상도를 512 → **1024**로 올려 AI가 따라 그릴 실루엣을 선명하게 했다.
- 형상 가이드를 **불투명 흰 배경**으로 바꾸고 캡처 동안 뷰 트랜스폼을 Standard로 고정했다. 투명 배경은 Provider마다 다르게 합성되고, AgX가 흰색을 0.77 회색으로 눌러 실루엣 대비를 깎고 있었다(측정: 밝은 픽셀 0개 → 831,302개). contact sheet 여백도 같은 흰색으로 채워 이음매를 없앴다.
- 분석 단계에서 **자세, 포즈, 손동작, 카메라 각도, 구도, 실루엣, 비율, 체형을 기록하지 않도록** 했고, 생성 프롬프트에는 `style`, `surface_regions`, `design_rules`, `object_summary`만 싣는다. 참조의 자세 서술이 분석 텍스트를 타고 생성 형상으로 새는 통로를 막는다.
- 형상 계약에서 팔·다리 각도 같은 **자세 부위 열거를 뺐다**. 부위를 열거하자 이미지 모델이 오히려 참조의 손가락 가리키는 동작을 만들어 냈고, 그 팔 때문에 정면 뷰 경계 상자가 늘어나 어깨가 배경에 매핑됐다. 실루엣 밖에 아무것도 그리지 말라는 조건만 남겼다.
- 형상 가이드의 검은 외곽선 스트로크는 도입했다가 되돌렸다. 가이드가 "일러스트 대상"처럼 보여 이미지 모델이 실루엣을 따라 그리는 대신 다시 그리는 쪽으로 기울었다.

### 수정

- 어깨·허리·부츠 주변에 **흰색 빗살 얼룩**이 남던 문제 해결. 생성 그림의 실루엣이 모델보다 좁으면 표본이 배경으로 나가는데, 기존에는 표본을 중심 쪽으로 3.5%/7.5%/13% 단계로 끌어당겨 재시도했다. 이웃 픽셀이 서로 다른 단계에서 성공해 전혀 다른 위치를 읽는 바람에 찢어진 줄무늬가 생겼다. 이제 각 생성 이미지마다 chamfer 스캔 두 번으로 **최근접 전경 색으로 채운 버퍼와 전경까지의 거리**를 미리 만들어, 경계 밖 표본은 공간적으로 연속된 색으로 잇고 피사체에서 멀면(피사체 크기의 6% 초과) 다른 시점에 양보한다.
- `참조 이미지 추가`의 파일 선택기에서 `AttributeError: 'UVMAPPING_OT_add_reference_images' object has no attribute 'directory'`로 추가가 실패하던 문제 해결. `ImportHelper`가 제공하지 않는 `directory` 속성을 직접 선언했다.

### 변경 (기본값)

- `텍스처 크기` 기본값을 2048에서 **1024**로 낮췄다. CPU 베이크 시간과 결과 품질의 균형점이며, 이미 저장한 `.blend`는 그 파일에 저장된 값을 계속 사용한다.

### 추가

- **대상 객체 등록 필드**: 패널에서 텍스처 대상 Mesh를 명시 등록한다. 등록이 있으면 선택과 무관하게 그 객체들만 대상이 되고, 비어 있으면 기존처럼 현재 선택을 따른다.
- **생성 후 자동 적용**(기본 켜짐): 3면도 생성이 끝나면 Diffuse/Albedo 베이크와 머티리얼 적용까지 이어서 실행한다.
- Edit·Paint 모드에서 실행하면 Object Mode로 자동 전환한 뒤 진행한다.

### 변경

- 모델 contact sheet를 생성 요청과 같은 21:9로 만들어 AI 캔버스와 좌표계를 일치시키고, 프롬프트에 입력 실루엣·부위 경계를 그대로 따르라는 제약을 추가해 투영 위치 정확도를 높였다.
- 모든 시점에서 가려진 면(다리 안쪽 등)은 전체 평균색 대신 같은 시점의 같은 좌표 색을 다시 읽는다. 평균색으로 뭉개지던 픽셀이 사라진다. 베이크 통계에 `occluded_fallback_pixels`를 추가했다.

## 2.1.1 - 2026-09-10

### 수정

- 3면도 생성이 "생성 중…"에서 멈춘 채 결과가 반영되지 않던 문제 해결. Blender가 `execute()` 반환 즉시 Operator RNA를 해제하는데 감시 타이머가 그 인스턴스를 참조해 `ReferenceError`로 조용히 멈췄다. 진행 중 작업 상태를 연산자 밖 `_TextureRun`으로 분리해 3면 분할, 디자인 상태 기록, `Diffuse/Albedo 적용` 버튼 활성화가 정상 진행된다.
- 타이머가 바꾼 상태 문자열이 즉시 보이도록 패널 다시 그리기 추가
- Subdivision, Mirror, Bevel, Triangulate처럼 topology를 바꾸는 Modifier가 있으면 3면도 생성 뒤 `Diffuse/Albedo 적용`이 항상 실패해 텍스처가 적용·저장되지 않던 문제 해결. 베이크가 평가 Mesh의 UV를 그대로 투영한다.
- 평가 UV가 0-1 Atlas 밖으로 나간 삼각형 수를 베이크 결과에 기록하고 경고로 알림
- Edit·Paint 모드에서는 원본 Mesh가 최신이 아니므로 3면도 생성과 베이크를 AI 호출 전에 막고 패널에 안내. 이전에는 AI 비용을 쓴 뒤 베이크 단계에서야 실패했다.

## 2.1.0 - 2026-09-08

### 변경 (호환성 깨짐)

- Gemini와 OpenAI 개인 키 두 개를 **OpenRouter API 키 하나**로 통합하고 환경설정 입력 필드를 단일화
- 참조 분석은 OpenRouter `/chat/completions`, 3면도 생성은 `/images` 엔드포인트 하나로 통일
- 환경 변수 대체 키를 `GEMINI_API_KEY`/`OPENAI_API_KEY`에서 `OPENROUTER_API_KEY`로 변경
- 모델 식별자를 OpenRouter 슬러그로 교체하고 프리셋 전환 시 분석·이미지 모델을 함께 갱신
  - Nano Banana Pro: `google/gemini-3.7-flash` + `google/gemini-3-pro-image`
  - GPT Image: `openai/gpt-5.6-sol` + `openai/gpt-5.4-image-2`
- 고급 설정에서 openrouter.ai/models의 임의 모델 식별자를 직접 입력 가능

### 제거

- `uvmapping/openai_provider.py`와 Gemini generateContent 전용 payload 빌더 삭제
- 환경설정의 `gemini_api_key`, `openai_api_key` 및 Provider별 모델 속성 4종 삭제

## 2.0.0 - 2026-09-08

### 제거 (호환성 깨짐)

- 자동 Seam 분석, 자동 UV 언랩, Seam 미리보기, UV 재패킹 기능 전체 제거
- `uvmapping.auto_unwrap`, `uvmapping.analyze`, `uvmapping.preview_seams`, `uvmapping.clear_preview`, `uvmapping.repack_uvs` 연산자 삭제
- `UV 언랩` 패널과 프리셋·품질·Seam 정책·분석 가중치 등 관련 설정 삭제

### 변경

- AI 텍스처 파이프라인이 사용자가 이미 펼쳐 둔 활성 UV에서 TextureJob 계약을 직접 생성
- 3면도 생성과 베이크 시점에 각각 현재 UV로 계약을 다시 계산해 그 사이의 UV·메시 변경을 감지
- UV 맵 누락, 0-1 범위 이탈, 객체 간 UV 겹침을 생성 전에 안내와 함께 차단
- 텍스처 크기와 UV 패딩 설정을 `AI 손맵 텍스처` 패널로 이동

## 1.2.0 - 2026-08-30

- 자유 참조 이미지의 공통 손맵 스타일을 구조화해 분석하는 Gemini 연동 추가
- 선택 모델의 FRONT/RIGHT/BACK contact sheet와 단일 호출·단일 이미지 3면도 생성 추가
- 생성된 3면도를 로컬에서 세 방향 이미지로 분리하고 객체에 디자인 상태 저장
- GPT-Image-2 기반 OpenAI 분석·단일 3면도 생성 Provider 추가
- Gemini와 OpenAI API 키를 `.blend`가 아닌 Blender 개인 애드온 환경설정으로 이동
- macOS와 Windows에서 Pinterest 등 브라우저 클립보드 이미지를 참조로 바로 붙여넣는 기능 추가
- Blender 한글 IME 오류를 우회하는 OS 네이티브 추가 지시 입력 창 추가
- Nano Banana Pro와 GPT-Image-2(덕테이프)를 바로 고르는 이미지 생성 모델 선택 버튼 추가
- FRONT/RIGHT/BACK 깊이 인식 투영과 좌측 대칭 보완을 사용한 Diffuse/Albedo UV Atlas 베이크 추가
- 생성 결과를 sRGB PNG와 Principled BSDF 머티리얼로 적용하고 무과금 재베이크 지원
- GitHub Pages 기반 Blender Extension 원격 설치 및 자동 업데이트 저장소 추가
- Release 자산 lock, SHA-256 검증과 공식 Blender 인덱스 자동 생성 워크플로우 추가

## 1.1.0 - 2026-08-30

- 선택한 여러 Mesh의 UV를 하나의 0–1 공유 Atlas에 일괄 패킹
- 텍스처 해상도와 픽셀 패딩으로 이해하기 쉬운 패킹 여백 설정
- 공유 Atlas 패킹 뒤 품질과 TextureJob UV 해시 재계산
- 독립 패킹 모드에서도 동일한 픽셀 기준 FRACTION 여백 적용

## 1.0.0 - 2026-08-30

- 다중 Seam 후보를 임시 Mesh에서 언랩하고 품질 점수로 최적 결과 선택
- 다중 오브젝트 및 공유 Mesh datablock의 트랜잭션 처리
- 원본 Seam을 변경하지 않는 Seam 후보 미리보기
- UV 품질 보고서와 후속 AI 텍스처 단계용 TextureJob JSON 계약
- 대형 메시용 overlap 검사 예산과 결정론적 품질 평가

## 0.1.0 - 2026-08-29

- 자동 Seam 생성, Unwrap, Pack을 수행하는 초기 연산자
- macOS 및 Windows용 격리 개발 프로필 실행기
- 정적 프로젝트 검사와 실제 Blender 스모크 테스트
