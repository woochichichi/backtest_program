@echo off
setlocal enabledelayedexpansion
title KRX 백테스터 (개발 모드)

rem ==================================================================
rem  개발 모드 - 코드를 고치면 서버가 알아서 다시 뜬다
rem  - 검은 창에 로그와 오류가 그대로 보인다
rem  - engine/ server/ 의 .py 를 저장하면 자동 재시작
rem  - server/web/ 의 html/css/js 는 브라우저 새로고침(Ctrl+F5)만 하면 된다
rem  - 끌 때는 이 창에서 Ctrl+C 를 누르거나 창을 닫는다
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "PYEXE=!ROOT!\.venv\Scripts\python.exe"
set "SRVHOST=127.0.0.1"
set "SRVPORT=8000"

echo.
echo  ==========================================
echo   KRX 백테스터 - 개발 모드
echo  ==========================================
echo.

if not exist "!PYEXE!" (
    echo  [오류] 가상환경이 없습니다. install.bat 을 먼저 실행하세요.
    goto :end
)

"!PYEXE!" -c "import fastapi, uvicorn" >nul 2>&1
if !errorlevel! neq 0 (
    echo  [오류] fastapi 또는 uvicorn 이 설치되어 있지 않습니다.
    echo         install.bat 을 다시 실행하세요.
    goto :end
)

rem  KRX백테스터.vbs 로 켜 둔 서버가 있으면 포트가 겹친다. 먼저 정리한다.
if exist "!ROOT!\.server.pid" (
    set /p SRVPID=<"!ROOT!\.server.pid"
    echo  이미 켜져 있던 서버를 종료합니다 ^(PID !SRVPID!^).
    taskkill /PID !SRVPID! /T /F >nul 2>&1
    del /q "!ROOT!\.server.pid" >nul 2>&1
)

echo  주소 : http://!SRVHOST!:!SRVPORT!/
echo.
echo  코드를 저장하면 서버가 자동으로 다시 뜹니다.
echo  화면 파일^(html/css/js^)만 고쳤다면 브라우저에서 Ctrl+F5 만 누르세요.
echo  끝낼 때는 이 창에서 Ctrl+C 를 누르거나 창을 닫으세요.
echo.
echo  ------------------------------------------
echo.

start "" "http://!SRVHOST!:!SRVPORT!/"
"!PYEXE!" -m uvicorn server.app:app --host !SRVHOST! --port !SRVPORT! --reload --reload-dir "!ROOT!\engine" --reload-dir "!ROOT!\server"

:end
echo.
pause
endlocal
