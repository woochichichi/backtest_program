@echo off
setlocal enabledelayedexpansion
title KRX 백테스터 프로그램 업데이트

rem ==================================================================
rem  프로그램 업데이트 (git pull)
rem  - 주가 데이터가 아니라 "프로그램 코드"를 최신으로 받습니다.
rem  - 주가 데이터 갱신은 update_marcap.bat 입니다. 서로 다릅니다.
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "VENV=!ROOT!\.venv"
set "PYEXE=!VENV!\Scripts\python.exe"
set "REQ=!ROOT!\requirements.txt"

echo.
echo  ==========================================
echo   KRX 백테스터 프로그램 업데이트
echo  ==========================================
echo.
echo  이 창은 프로그램 코드를 최신으로 받습니다.
echo  주가 데이터는 그대로 두므로 다시 받지 않습니다.
echo.

rem ---- git 확인 ----
where git >nul 2>&1
if !errorlevel! neq 0 (
    echo  [오류] git 을 찾을 수 없습니다.
    echo         https://git-scm.com/download/win 에서 설치한 뒤 다시 실행하세요.
    goto :end
)

if not exist "!ROOT!\.git" (
    echo  [오류] 이 폴더는 git 으로 받은 폴더가 아닙니다.
    echo         업데이트할 수 없습니다. 담당자에게 문의하세요.
    goto :end
)

rem ---- 현재 버전 ----
set "OLDREV="
for /f %%a in ('git -C "!ROOT!" rev-parse --short HEAD') do set "OLDREV=%%a"
echo  현재 버전 : !OLDREV!
echo.

rem ---- 서버가 켜져 있으면 먼저 끈다 ----
echo  [1/4] 실행 중인 서버가 있으면 종료합니다.
if exist "!ROOT!\.server.pid" (
    set /p SRVPID=<"!ROOT!\.server.pid"
    taskkill /PID !SRVPID! /T /F >nul 2>&1
    del /q "!ROOT!\.server.pid" >nul 2>&1
)
rem  wmic 은 최신 Windows 에서 제거되어 쓰지 않는다.
rem  VBS 로 켠 경우 위의 PID 종료로 충분하고,
rem  파이썬 소스는 잠기지 않으므로 pull 자체는 어차피 성공한다.
echo  [1/4] 완료.
echo.

rem ---- 로컬 변경 확인 ----
echo  [2/4] 내 컴퓨터에서 바뀐 파일이 있는지 확인합니다.
set "DIRTY="
for /f "delims=" %%a in ('git -C "!ROOT!" status --porcelain') do set "DIRTY=1"

if defined DIRTY (
    echo.
    echo  아래 파일이 이 컴퓨터에서 바뀌어 있습니다.
    echo  ------------------------------------------
    git -C "!ROOT!" status --short
    echo  ------------------------------------------
    echo.
    echo  업데이트를 계속하려면 이 변경 내용을 버려야 합니다.
    echo  직접 고친 내용이 있다면 지금 백업하세요.
    echo.
    echo  전략 파일^(strategies 폴더^)은 버려도 다시 만들 수 있습니다.
    echo  주가 데이터^(marcap 폴더^)와 로그는 영향을 받지 않습니다.
    echo.
    set "YN="
    set /p "YN=  변경 내용을 버리고 계속할까요? [Y/N]: "
    if /i not "!YN!"=="Y" (
        echo.
        echo  업데이트를 취소했습니다. 아무것도 바뀌지 않았습니다.
        goto :end
    )
    git -C "!ROOT!" reset --hard >nul 2>&1
    echo  변경 내용을 되돌렸습니다.
)
echo  [2/4] 완료.
echo.

rem ---- 받기 ----
echo  [3/4] 최신 프로그램을 받습니다.
git -C "!ROOT!" pull
if !errorlevel! neq 0 (
    echo.
    echo  [오류] 받기에 실패했습니다.
    echo         인터넷 연결을 확인하거나 담당자에게 문의하세요.
    goto :end
)
echo  [3/4] 완료.
echo.

rem ---- 패키지 동기화 ----
echo  [4/4] 필요한 패키지를 확인합니다.
if not exist "!PYEXE!" (
    echo  가상환경이 없습니다. install.bat 을 먼저 실행하세요.
    goto :done
)
if not exist "!REQ!" goto :done
"!PYEXE!" -m pip install -q -r "!REQ!"
if !errorlevel! neq 0 (
    echo.
    echo  [경고] 패키지 설치에 실패했습니다. install.bat 을 다시 실행해 보세요.
    goto :end
)
echo  [4/4] 완료.

:done
set "NEWREV="
for /f %%a in ('git -C "!ROOT!" rev-parse --short HEAD') do set "NEWREV=%%a"
echo.
echo  ==========================================
if "!OLDREV!"=="!NEWREV!" (
    echo   이미 최신입니다
    echo  ==========================================
    echo.
    echo  버전 : !NEWREV!  ^(변경 없음^)
) else (
    echo   업데이트 완료
    echo  ==========================================
    echo.
    echo  !OLDREV!  ->  !NEWREV!
    echo.
    echo  바뀐 내용
    echo  ------------------------------------------
    git -C "!ROOT!" log --oneline !OLDREV!..!NEWREV!
    echo  ------------------------------------------
)
echo.
echo  이제 KRX백테스터.vbs 를 더블클릭해 실행하세요.
echo.
:end
echo.
pause
endlocal
