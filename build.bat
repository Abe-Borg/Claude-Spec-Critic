@echo off
REM build.bat - Build spec-review.exe using PyInstaller
REM Run this from the project root directory

echo ========================================
echo Building spec-review.exe
echo ========================================
echo.

REM Check if PyInstaller is installed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
    echo.
)

REM Clean previous builds
if exist "dist" (
    echo Cleaning previous build...
    rmdir /s /q dist
)
if exist "build" (
    rmdir /s /q build
)

REM Build the executable
echo Building executable...
echo.
pyinstaller spec-review.spec --clean

if errorlevel 1 (
    echo.
    echo ========================================
    echo BUILD FAILED
    echo ========================================
    exit /b 1
)

echo.
echo ========================================
echo BUILD SUCCESSFUL
echo ========================================
echo.
echo Executable created at: dist\spec-review.exe
echo.
echo To use:
echo   1. Copy dist\spec-review.exe to any folder
echo   2. Set your API key: set ANTHROPIC_API_KEY=your-key
echo   3. Run: spec-review.exe review -i ./specs -o ./output
echo.
