# UVmapping Blender

메시의 형상 흐름과 곡률을 분석해 Seam을 생성하고 UV Unwrap과 Pack을 한 번에 처리하는 Blender Extension입니다. 첫 개발 단계는 결정론적인 형상 분석 기반 자동 언랩에 집중하며, AI 텍스처 매핑은 UV 코어가 안정된 뒤 별도 계층으로 추가합니다.

## 현재 개발 범위

- 기존 Seam을 보존하거나 새 Seam 후보를 생성
- 메시 경계, Sharp, 재질 경계, 이면각을 이용한 Seam 점수 계산
- Blender 언랩 연산과 Island 패킹
- 대표 메시를 이용한 실제 Blender 스모크 테스트

자동 처리는 좋은 초안을 빠르게 만드는 도구입니다. 모든 메시에서 수작업 수정이 전혀 필요 없는 결과를 보장하지는 않습니다.
현재 MVP는 UV 겹침 방지를 우선하므로 원통의 캡 경계나 토러스처럼 곡률이 반복되는 메시에서 Seam이 많이 생성될 수 있습니다. 이후 차트 단위 영역 성장과 왜곡 피드백으로 Seam 수를 줄일 예정입니다.

## 요구 사항

- Blender 4.5 LTS 이상
- macOS 또는 Windows

## 격리 개발 프로필

개발 실행기는 평소 사용하는 Blender 설정과 설치된 Extension을 건드리지 않습니다. 저장소 자체를 전용 프로필의 `extensions/user_default/uvmapping_blender`에 연결하므로 소스를 수정한 뒤 ZIP을 다시 설치할 필요가 없습니다.

### macOS

기본 Blender 경로는 `/Applications/Blender.app/Contents/MacOS/Blender`입니다.

```bash
bash scripts/dev_run.sh
```

다른 Blender 실행 파일은 프로젝트 전용 환경 변수로 지정합니다.

```bash
UVMAPPING_BLENDER_BINARY="/경로/Blender.app/Contents/MacOS/Blender" bash scripts/dev_run.sh
```

전용 프로필은 `~/Library/Application Support/Blender/UVmappingBlenderDev/<BlenderVersion>`에 생성됩니다. Blender 인자는 그대로 전달할 수 있습니다.

```bash
bash scripts/dev_run.sh --background --python tests/blender_smoke.py
```

### Windows

프로젝트 전용 포터블 Blender의 `blender.exe` 경로를 지정합니다. 실행 파일 옆 `portable` 디렉터리만 사용하며 일반 Blender 사용자 설정에는 링크를 만들지 않습니다.

```powershell
$env:UVMAPPING_BLENDER_BINARY = 'C:\Tools\UVmappingBlender\blender.exe'
.\scripts\dev_run.ps1
```

명령 프롬프트에서는 같은 인자를 배치 래퍼로 전달합니다.

```bat
set UVMAPPING_BLENDER_BINARY=C:\Tools\UVmappingBlender\blender.exe
scripts\dev_run.bat --mode Background -- --python tests\blender_smoke.py
```

지원 모드는 `Gui`, `Link`, `Background`, `Expression`, `File`입니다. 예를 들어 `--mode File --script tests\blender_smoke.py`로 Python 파일을 실행할 수 있습니다.

## 검사

호스트 Python에서 프로젝트 구조와 실행기 안전 조건을 검사합니다.

```bash
python3 tests/check_project.py
python3 tests/test_analysis_pure.py
```

실제 Blender에서는 격리 프로필로 등록, 대표 메시 자동 언랩, UV와 Seam, 유한 좌표, 등록 해제를 확인합니다.

```bash
bash scripts/dev_run.sh --background --python tests/blender_smoke.py
bash scripts/dev_run.sh --background --python tests/blender_quality.py
```

품질 검사는 Cube, Cylinder, Sphere, Torus에서 UV 삼각형의 양의 면적 겹침이 없는지와 Seam 비율 상한을 데이터 API로 확인합니다. Blender의 `uv.select_overlap`은 백그라운드 모드에서 사용하지 않습니다.

## 사용자용 원격 설치와의 구분

위 개발 프로필은 로컬 소스 연결 전용이며 사용자 배포 URL이 아닙니다. 현재 공개 Extension 저장소, GitHub Release, 원격 `index.json`은 발행하지 않았습니다. 향후 릴리스가 승인되어 원격 저장소가 배포되면, 사용자는 Blender의 `Get Extensions` 저장소 설정에 공지된 URL을 추가해 설치·업데이트하게 됩니다.

## 라이선스

GPL-3.0-or-later. 자세한 내용은 [LICENSE](LICENSE)를 참조하세요.
