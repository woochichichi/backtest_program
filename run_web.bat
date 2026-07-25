@echo off
setlocal enabledelayedexpansion
title KRX Backtester 웹 서버

rem ==================================================================
rem  KRX Backtester 웹 서버 실행
rem  - .venv 확인 -> uvicorn 기동 -> 2초 뒤 브라우저 자동 열기
rem  - 서버를 끄려면 이 창에서 Ctrl+C 를 누르거나 창을 닫으세요.
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "VENV=!ROOT!\.venv"
set "PYEXE=!VENV!\Scripts\python.exe"
set "SRVHOST=127.0.0.1"
set "SRVPORT=8000"
set "URL=http://!SRVHOST!:!SRVPORT!"

echo.
echo  ==========================================
echo   KRX Backtester 웹 서버
echo  ==========================================
echo.

rem ---- 가상환경 확인 ----
if not exist "!PYEXE!" (
    echo  [오류] 가상환경이 없습니다.
    echo         경로 : !VENV!
    echo.
    echo         install.bat 을 먼저 실행해 주세요.
    goto :end
)

rem ---- 필수 패키지 확인 ----
"!PYEXE!" -c "import fastapi, uvicorn" >nul 2>&1
if !errorlevel! neq 0 (
    echo  [오류] fastapi 또는 uvicorn 이 설치되어 있지 않습니다.
    echo.
    echo         install.bat 을 다시 실행해 주세요.
    goto :end
)

rem ---- 서버 코드 확인 ----
if not exist "!ROOT!\server\app.py" (
    echo  [오류] server\app.py 를 찾을 수 없습니다.
    echo         경로 : !ROOT!
    goto :end
)

rem ---- 데이터 안내 ----
if not exist "!ROOT!\marcap\data" (
    echo  [안내] marcap 시세 데이터가 아직 없습니다.
    echo         화면은 열리지만 백테스트는 실행되지 않습니다.
    echo         update_marcap.bat 을 실행해 데이터를 먼저 받으세요.
    echo.
)

rem ---- API 키 안내 ----
if not defined ANTHROPIC_API_KEY (
    echo  [안내] ANTHROPIC_API_KEY 가 없어 AI 전략 생성만 사용할 수 없습니다.
    echo         setx ANTHROPIC_API_KEY sk-ant-여기에키
    echo         위 명령을 실행한 뒤 이 창을 새로 여세요.
    echo.
)

cd /d "!ROOT!"

echo  주소 : !URL!
echo  종료 : 이 창에서 Ctrl+C
echo.
echo  잠시 후 기본 브라우저가 자동으로 열립니다.
echo.

rem ---- 2초 뒤 브라우저 열기 ^(별도 창에서 대기^) ----
start "KRXBacktesterOpen" /min cmd /c "ping -n 3 127.0.0.1 >nul & start !URL!"

rem ---- 서버 기동 ^(이 창을 점유합니다^) ----
"!PYEXE!" -m uvicorn server.app:app --host !SRVHOST! --port !SRVPORT!

echo.
echo  서버가 종료되었습니다.

:end
echo.
pause
endlocal
