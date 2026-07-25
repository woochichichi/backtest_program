@echo off
setlocal enabledelayedexpansion
title marcap 매일 자동 갱신 등록

rem ==================================================================
rem  Windows 작업 스케줄러에 매일 자동 갱신을 등록한다.
rem  관리자 권한이 없어도 현재 사용자 계정으로 등록된다.
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "TASK=KRXBacktesterDataSync"
set "TARGET=!ROOT!\update_marcap.bat"

echo.
echo  ==========================================
echo   marcap 매일 자동 갱신 등록
echo  ==========================================
echo.

if not exist "!TARGET!" (
    echo  [오류] update_marcap.bat 을 찾을 수 없습니다: !TARGET!
    echo         이 파일을 프로젝트 루트에 함께 두고 실행하세요.
    goto :end
)

echo  갱신 시각을 입력하세요. 형식은 24시간제 HH:MM 입니다.
echo  장 마감 후인 18:30 을 권장합니다. 그냥 Enter 를 누르면 18:30 으로 등록합니다.
echo.
set "RUNTIME="
set /p "RUNTIME=  갱신 시각 [18:30]: "
if "!RUNTIME!"=="" set "RUNTIME=18:30"

echo !RUNTIME!| findstr /r "^[0-2][0-9]:[0-5][0-9]$" >nul
if !errorlevel! neq 0 (
    echo.
    echo  [오류] 시각 형식이 올바르지 않습니다: !RUNTIME!
    echo         예^) 18:30  09:05  23:00
    goto :end
)

echo.
echo  등록 정보
echo    작업 이름 : !TASK!
echo    실행 파일 : !TARGET!
echo    실행 주기 : 매일 !RUNTIME!
echo.

schtasks /Query /TN "!TASK!" >nul 2>&1
if !errorlevel! equ 0 (
    echo  기존에 등록된 작업이 있어 새 설정으로 덮어씁니다.
)

schtasks /Create /TN "!TASK!" /TR "\"!TARGET!\" /silent" /SC DAILY /ST !RUNTIME! /F
if !errorlevel! neq 0 (
    echo.
    echo  [오류] 작업 스케줄러 등록에 실패했습니다.
    echo         이 창을 관리자 권한으로 다시 실행해 보세요.
    goto :end
)

echo.
echo  [완료] 매일 !RUNTIME! 에 marcap 데이터가 자동 갱신됩니다.
echo.
echo  확인   : schtasks /Query /TN "!TASK!" /V /FO LIST
echo  즉시실행: schtasks /Run   /TN "!TASK!"
echo  해제   : remove_daily_update.bat
echo.
:end
pause
endlocal
