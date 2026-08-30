# 변경 기록

이 프로젝트는 [Semantic Versioning](https://semver.org/)을 따릅니다.

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
