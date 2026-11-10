$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = if ($env:GALLERY_PYTHON) { $env:GALLERY_PYTHON } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "의존성 설치 실패" }
& $python -m py_compile main.py gui.py database_viewer.py app_icon.py
if ($LASTEXITCODE -ne 0) { throw "Python 소스 검증 실패" }
& $python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 설치 실패" }

# PySide6 plugins are collected automatically by PyInstaller's PySide6 hooks.
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name GalleryMonitor --icon "assets\penguin_icon.ico" --add-data "assets\penguin_icon.png;assets" --distpath dist --workpath build gui.py
if ($LASTEXITCODE -ne 0) { throw "GalleryMonitor.exe 빌드 실패" }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name GalleryDatabaseViewer --icon "assets\penguin_icon.ico" --add-data "assets\penguin_icon.png;assets" --distpath dist --workpath build_db database_viewer.py
if ($LASTEXITCODE -ne 0) { throw "GalleryDatabaseViewer.exe 빌드 실패" }

# Original GUI provides buttons to open these deployment documents. Keep them
# beside the EXE because BASE_DIR points to sys.executable.parent for frozen apps.
Copy-Item -Force readme.txt dist\readme.txt
Copy-Item -Force update.txt dist\update.txt

Write-Host "빌드 완료: dist\GalleryMonitor.exe, dist\GalleryDatabaseViewer.exe"
Write-Host "동봉 문서: dist\readme.txt, dist\update.txt"
