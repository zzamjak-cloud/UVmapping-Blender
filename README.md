# UVmapping Blender

선택한 메시의 Seam 생성부터 UV 언랩과 패킹까지 한 번에 처리하는 Blender Extension입니다. 유기체와 하드서페이스에 맞는 설정을 선택하고 결과를 미리 확인할 수 있습니다.

## 요구 사항

- Blender 4.5 LTS 이상
- 편집 가능한 Mesh 객체

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

1. Object Mode에서 UV를 만들 Mesh 객체를 모두 선택합니다. 선택하지 않은 객체는 처리되지 않습니다.
2. `3D Viewport > Sidebar(N) > UV Mapping`에서 `UV 언랩` 패널을 엽니다.
3. 메시 형태에 맞는 `프리셋`과 처리할 후보 수를 정하는 `품질`을 선택합니다.
4. `출력 텍스처`에서 `텍스처 크기`와 `UV 패딩`을 정합니다. `선택 객체를 한 장에 배치`를 켜면 선택된 메시의 UV를 하나의 텍스처 공간에 함께 패킹합니다.
5. `Seam 보기`를 눌러 후보를 확인합니다. 미리보기를 없애려면 `Seam 숨기기`를 누릅니다.
6. `UV 언랩`을 누르면 선택된 모든 Mesh에 Seam 생성, 언랩, 패킹이 실행됩니다.
7. 완료된 UV는 기본적으로 `AutoUV` UV 레이어에서 확인할 수 있습니다.

## 라이선스

GPL-3.0-or-later. 자세한 내용은 [LICENSE](LICENSE)를 참조하세요.
