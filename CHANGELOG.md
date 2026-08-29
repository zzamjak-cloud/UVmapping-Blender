# 변경 기록

이 프로젝트는 [Semantic Versioning](https://semver.org/)을 따릅니다.

## 다음 버전

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
