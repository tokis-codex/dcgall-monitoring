# 갤러리 모니터 v80 — GitHub 배포 안내

이 폴더는 현재 PySide6 소스를 그대로 포함한 **GitHub 저장소용 패키지**입니다. GitHub Actions가 Windows x64 환경에서 두 개의 EXE를 빌드하고 포터블 ZIP을 생성합니다. 이 소스 ZIP 자체는 Windows 실행 파일이 아닙니다.

## 저장소에 올리기

1. 이 GitHub용 ZIP을 **압축 해제**하세요. ZIP 파일 자체만 저장소에 올리는 것이 아니라, 압축을 푼 **폴더의 내용 전체** (`gui.py`, `.github`, `assets` 등)를 저장소 최상위에 올립니다. `.github`는 숨김 폴더이므로 빠뜨리지 마세요.
2. GitHub에서 새 빈 저장소를 만들고, 로컬에서 아래 명령으로 파일을 올립니다(저장소 주소는 본인 주소로 교체).

```powershell
git init
git add .
git commit -m "Add Gallery Monitor v80 PySide6 source and Windows release workflow"
git branch -M main
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

## 실행 파일 빌드 (버튼으로 실행)

GitHub 저장소의 **Actions → Windows portable release (v80) → Run workflow**를 선택하세요. 완료 후 해당 실행의 **Artifacts**에서 `GalleryMonitor-Windows-x64`를 받을 수 있습니다. 이 방식은 GitHub Releases에 자동 게시하지 않습니다.

## GitHub Releases에 자동 게시

다음 명령으로 `v80` 태그를 올리면 Windows 빌드 후 **Releases**에 ZIP과 SHA256 체크섬을 게시합니다. 향후 수정 버전은 `v80.1`, `v80.2` 같은 새 태그를 사용하면 됩니다.

```powershell
git tag v80
git push origin v80
```

릴리스 ZIP에는 `GalleryMonitor.exe`, `GalleryDatabaseViewer.exe`, `readme.txt`, `update.txt`만 포함됩니다. 사용자는 ZIP 압축을 풀고 `GalleryMonitor.exe`를 실행하면 됩니다. Python은 필요하지 않습니다.

**중요:** 배포 전 GitHub Actions의 완료 여부와 Windows PC에서 실제 실행을 확인하세요. 현재 이 패키지 자체에는 빌드된 EXE가 들어 있지 않습니다.

## 개인 정보 및 기존 데이터

- **`config.json`에는 Telegram Bot Token, Chat ID가 포함될 수 있습니다.** 절대로 GitHub에 올리지 마세요.
- **`monitor_state.sqlite3`에는 수집한 게시글과 작성자 식별 정보가 포함될 수 있습니다.** GitHub에 올리지 마세요.
- `.gitignore`에 위 파일과 빌드 산출물이 제외되도록 설정했습니다. 다만 이미 Git에 추적 중인 파일은 `.gitignore`만으로 제거되지 않으니 별도로 점검해야 합니다.
- 앱을 사용하던 기존 설정과 DB를 새 EXE 폴더로 **직접** 복사하면 이어서 사용할 수 있습니다. 배포 ZIP에는 사용자 데이터가 포함되지 않습니다.
- GitHub Actions 워크플로에 `contents: write` 권한이 필요합니다. 저장소 설정의 Actions 권한 정책이 이를 제한하면 변경이 필요할 수 있습니다.
- 공개 배포를 위한 소프트웨어 라이선스는 사용자가 직접 결정해야 합니다. 원본 소스에 없던 라이선스는 임의로 추가하지 않았습니다.

## 로컬 Windows 빌드

기존처럼 Windows에서 `build_windows.cmd`를 실행하면 두 개의 EXE가 `dist` 폴더에 생성됩니다. CI에서는 Python 버전을 확실히 선택하기 위해 `GALLERY_PYTHON=python` 환경 변수만 설정합니다. 모니터링 및 GUI 동작 코드는 변경하지 않았습니다.
