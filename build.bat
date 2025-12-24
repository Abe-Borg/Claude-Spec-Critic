@echo off
REM build.bat - Build MEP-Spec-Review.exe using PyInstaller
REM Run this from the project root directory (Claude-Spec-Critic)

echo.
echo ========================================
echo    MEP Spec Review - Build Script
echo ========================================
echo.

REM Check Python version
python --version
echo.

REM Check if PyInstaller is installed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    pip install pyinstaller
    echo.
)

REM Clean previous builds
echo [INFO] Cleaning previous builds...
if exist "dist" rmdir /s /q dist
if exist "build" rmdir /s /q build
echo.

REM Build the executable
echo [INFO] Building executable (this may take 1-2 minutes)...
echo.
pyinstaller spec-review.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo ========================================
    echo    BUILD FAILED
    echo ========================================
    echo.
    echo Common issues:
    echo   - Missing dependencies: pip install -r requirements.txt
    echo   - Virtual environment not activated
    echo   - PyInstaller version mismatch
    echo.
    pause
    exit /b 1
)

echo.
echo ========================================
echo    BUILD SUCCESSFUL
echo ========================================
echo.
echo Output: dist\MEP-Spec-Review.exe
echo.

REM Show file size
for %%A in ("dist\MEP-Spec-Review.exe") do echo Size: %%~zA bytes (approx %%~zA bytes / 1048576 = ~MB)
echo.

echo ----------------------------------------
echo  DISTRIBUTION INSTRUCTIONS
echo ----------------------------------------
echo.
echo To distribute to colleagues:
echo.
echo   1. Copy dist\MEP-Spec-Review.exe to a shared folder
echo.
echo   2. Users need to either:
echo      a) Create spec_critic_api_key.txt with their API key
echo         in the same folder as the .exe
echo      OR
echo      b) Enter the API key in the GUI when prompted
echo.
echo   3. Double-click MEP-Spec-Review.exe to launch
echo.
echo No Python installation required on target machines!
echo.
pause
