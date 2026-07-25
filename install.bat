@echo off
setlocal enabledelayedexpansion
title KRX Backtester 설치

rem ==================================================================
rem  KRX Backtester 설치
rem  - 파이썬 확인 -> .venv 가상환경 생성 -> 의존성 설치
rem  - 이 파일을 더블클릭하면 됩니다. 관리자 권한은 필요 없습니다.
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "VENV=!ROOT!\.venv"
set "REQ=!ROOT!\requirements.txt"
set "PYEXE=!VENV!\Scripts\python.exe"

echo.
echo  ==========================================
echo   KRX Backtester 설치
echo  ==========================================
echo.
echo  설치 위치 : !ROOT!
echo  가상환경  : !VENV!
echo.

rem ---- [1/4] 파이썬 확인 ----
set "PYCMD="
where py >nul 2>&1
if !errorlevel! equ 0 (
    py -3 --version >nul 2>&1
    if !errorlevel! equ 0 set "PYCMD=py -3"
)
if not defined PYCMD (
    where python >nul 2>&1
    if !errorlevel! equ 0 set "PYCMD=python"
)
if not defined PYCMD (
    echo  [오류] 파이썬을 찾을 수 없습니다.
    echo.
    echo         아래 주소에서 Python 3.10 이상을 내려받아 설치하세요.
    echo         https://www.python.org/downloads/windows/
    echo.
    echo         설치 화면 맨 아래의 "Add python.exe to PATH" 를 반드시 체크하세요.
    echo         설치가 끝나면 이 창을 닫고 install.bat 을 다시 실행하세요.
    goto :end
)

echo  [1/4] 파이썬을 찾았습니다.
!PYCMD! --version
echo.

rem ---- [2/4] 가상환경 ----
if exist "!PYEXE!" (
    echo  [2/4] 기존 가상환경을 그대로 사용합니다.
) else (
    echo  [2/4] 가상환경을 만듭니다 ^(수십 초 걸립니다^).
    !PYCMD! -m venv "!VENV!"
    if !errorlevel! neq 0 (
        echo.
        echo  [오류] 가상환경 생성에 실패했습니다.
        echo         .venv 폴더를 지우고 다시 실행해 보세요.
        goto :end
    )
    echo  [2/4] 가상환경 생성 완료.
)

if not exist "!PYEXE!" (
    echo.
    echo  [오류] 가상환경 파이썬을 찾을 수 없습니다.
    echo         경로 : !PYEXE!
    goto :end
)
echo.

rem ---- [3/4] pip 갱신 ----
echo  [3/4] pip 을 최신 버전으로 올립니다.
"!PYEXE!" -m pip install --upgrade pip
echo.

rem ---- [4/4] 의존성 설치 ----
set "RC=0"
if exist "!REQ!" (
    echo  [4/4] requirements.txt 의 패키지를 설치합니다 ^(수 분 걸릴 수 있습니다^).
    "!PYEXE!" -m pip install -r "!REQ!"
    set "RC=!errorlevel!"
) else (
    echo  [4/4] requirements.txt 가 없어 기본 패키지를 설치합니다.
    "!PYEXE!" -m pip install fastapi uvicorn pandas numpy pyarrow anthropic
    set "RC=!errorlevel!"
)

echo.
if not "!RC!"=="0" (
    echo  ==========================================
    echo   설치 실패
    echo  ==========================================
    echo.
    echo  패키지 설치 중 오류가 났습니다. 위의 빨간 메시지를 확인하세요.
    echo  사내망이라면 프록시 설정이 필요할 수 있습니다.
    goto :end
)

echo  ==========================================
echo   설치 완료
echo  ==========================================
echo.
echo  다음 순서로 진행하세요.
echo.
echo    1. update_marcap.bat      - 시세 데이터 내려받기 ^(최초 1회, 수 분 소요^)
echo    2. run_web.bat            - 웹 서버 실행 + 브라우저 열기
echo    3. setup_daily_update.bat - 매일 자동 갱신 등록 ^(선택^)
echo.
echo  AI 전략 생성을 쓰려면 명령 프롬프트에서 아래를 한 번 실행하세요.
echo    setx ANTHROPIC_API_KEY sk-ant-여기에키
echo.

:end
echo.
pause
endlocal
