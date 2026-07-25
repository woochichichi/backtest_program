@echo off
setlocal enabledelayedexpansion
title marcap 자동 갱신 해제

set "TASK=KRXBacktesterDataSync"

echo.
echo  ==========================================
echo   marcap 자동 갱신 해제
echo  ==========================================
echo.

schtasks /Query /TN "!TASK!" >nul 2>&1
if !errorlevel! neq 0 (
    echo  등록된 자동 갱신 작업이 없습니다.
    goto :end
)

schtasks /Delete /TN "!TASK!" /F
if !errorlevel! neq 0 (
    echo.
    echo  [오류] 해제에 실패했습니다. 관리자 권한으로 다시 실행해 보세요.
    goto :end
)

echo.
echo  [완료] 자동 갱신이 해제되었습니다. 수동 갱신은 update_marcap.bat 으로 계속 가능합니다.
:end
echo.
pause
endlocal
