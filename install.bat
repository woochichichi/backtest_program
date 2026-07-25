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

rem ---- [1/4] 파이썬 확인 ^(3.10 ~ 3.13 지원^) ----
rem  numpy/pandas 는 정식 릴리스 버전에만 미리 빌드된 휠을 제공합니다.
rem  3.14 이상이나 베타 버전에서는 소스 빌드를 시도하다 컴파일러가 없어 실패합니다.
set "PYCMD="
where py >nul 2>&1
if !errorlevel! equ 0 (
    for %%v in (3.13 3.12 3.11 3.10) do (
        if not defined PYCMD (
            py -%%v --version >nul 2>&1
            if !errorlevel! equ 0 set "PYCMD=py -%%v"
        )
    )
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

rem ---- 버전 검증 ----
set "PYVER="
for /f "tokens=2" %%a in ('!PYCMD! --version 2^>^&1') do set "PYVER=%%a"
set "PYOK="
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    if "%%a"=="3" (
        if %%b geq 10 if %%b leq 13 set "PYOK=1"
    )
)
if not defined PYOK (
    echo  [오류] 지원하지 않는 파이썬 버전입니다: !PYVER!
    echo.
    echo         numpy / pandas 는 정식 릴리스 버전에만 미리 빌드된 파일을 제공합니다.
    echo         3.14 이상이나 베타^(b^) 버전에서는 직접 컴파일을 시도하다 실패합니다.
    echo.
    echo         해결 방법: Python 3.13 을 설치하세요.
    echo         https://www.python.org/downloads/release/python-3130/
    echo         페이지 하단의 "Windows installer ^(64-bit^)" 를 받으면 됩니다.
    echo         설치 화면 맨 아래 "Add python.exe to PATH" 를 반드시 체크하세요.
    echo.
    echo         기존 !PYVER! 를 지울 필요는 없습니다. 3.13 을 추가로 설치하면
    echo         이 스크립트가 자동으로 3.13 을 골라 씁니다.
    echo.
    echo         설치 후 이 창을 닫고 install.bat 을 다시 실행하세요.
    goto :end
)

echo  [1/4] 파이썬을 찾았습니다: !PYVER!
echo.

rem ---- [2/4] 가상환경 ----
rem  기존 가상환경이 지원하지 않는 파이썬으로 만들어졌으면 지우고 다시 만든다.
if not exist "!PYEXE!" goto :mkvenv

set "VENVVER="
for /f "tokens=2" %%a in ('"!PYEXE!" --version 2^>^&1') do set "VENVVER=%%a"
set "VENVOK="
for /f "tokens=1,2 delims=." %%a in ("!VENVVER!") do (
    if "%%a"=="3" (
        if %%b geq 10 if %%b leq 13 set "VENVOK=1"
    )
)
if defined VENVOK (
    echo  [2/4] 기존 가상환경을 그대로 사용합니다 ^(!VENVVER!^).
    goto :venvdone
)
echo  [2/4] 기존 가상환경이 지원하지 않는 버전입니다 ^(!VENVVER!^).
echo        지우고 다시 만듭니다.
rmdir /s /q "!VENV!"

:mkvenv
echo  [2/4] 가상환경을 만듭니다 ^(수십 초 걸립니다^).
!PYCMD! -m venv "!VENV!"
if !errorlevel! neq 0 (
    echo.
    echo  [오류] 가상환경 생성에 실패했습니다.
    echo         .venv 폴더를 직접 지우고 다시 실행해 보세요.
    goto :end
)
echo  [2/4] 가상환경 생성 완료.

:venvdone

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
echo    1. update_marcap.bat      - 시세 데이터 내려받기 ^(최초 1회, 10~30분^)
echo    2. KRX백테스터.vbs        - 이걸 더블클릭하면 끝입니다
echo                                검은 창 없이 켜지고 브라우저가 자동으로 열립니다.
echo                                한 번 더 누르면 종료할지 물어봅니다.
echo    3. setup_daily_update.bat - 매일 자동 갱신 등록 ^(선택^)
echo.
echo  앞으로는 KRX백테스터.vbs 하나만 쓰시면 됩니다.
echo  install.bat 은 이번 한 번으로 끝입니다.
echo.
echo  문제가 생기면 run_web.bat 을 실행해 보세요.
echo  검은 창에 오류 메시지가 그대로 보입니다 ^(로그: logs\server.log^).
echo.
echo  AI 전략 생성을 쓰려면 명령 프롬프트에서 아래를 한 번 실행하세요.
echo    setx ANTHROPIC_API_KEY sk-ant-여기에키
echo.

:end
echo.
pause
endlocal
