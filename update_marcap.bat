@echo off
setlocal enabledelayedexpansion
title marcap 데이터 갱신

rem ==================================================================
rem  marcap 데이터 갱신 (수동 실행 / 작업 스케줄러 공용)
rem  - marcap 폴더가 없으면 clone, 있으면 git pull
rem  - 완료 후 data_status.json 에 갱신 시각 기록 (웹에서 표시)
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "REPO=!ROOT!\marcap"
set "LOGDIR=!ROOT!\logs"
set "STATUS=!ROOT!\data_status.json"
set "GITURL=https://github.com/FinanceData/marcap.git"

if not exist "!LOGDIR!" mkdir "!LOGDIR!"
for /f "tokens=1-3 delims=-/. " %%a in ("%date%") do set "LOGDATE=%%a%%b%%c"
set "LOG=!LOGDIR!\marcap_sync_!LOGDATE!.log"

echo.
echo  ==========================================
echo   marcap 데이터 갱신
echo  ==========================================
echo.
>>"!LOG!" echo ---- %date% %time% ----

rem ---- git 존재 확인 ----
where git >nul 2>&1
if !errorlevel! neq 0 (
    echo  [오류] git 을 찾을 수 없습니다.
    echo         Git for Windows 설치 후 다시 실행하세요. https://git-scm.com/download/win
    >>"!LOG!" echo [ERROR] git not found
    set "RESULT=git_not_found"
    goto :finish
)

rem ---- clone 또는 pull ----
if not exist "!REPO!\.git" (
    echo  [1/2] marcap 저장소가 없습니다. 최초 clone 을 시작합니다 ^(수 분 소요^).
    >>"!LOG!" echo [INFO] clone start
    git clone "!GITURL!" "!REPO!" >>"!LOG!" 2>&1
    if !errorlevel! neq 0 (
        echo  [오류] clone 실패. 로그를 확인하세요: !LOG!
        set "RESULT=clone_failed"
        goto :finish
    )
    echo  [1/2] clone 완료.
    set "RESULT=cloned"
) else (
    echo  [1/2] 기존 저장소를 갱신합니다 ^(git pull^).
    >>"!LOG!" echo [INFO] pull start
    git -C "!REPO!" pull --ff-only >>"!LOG!" 2>&1
    if !errorlevel! neq 0 (
        echo  [경고] pull 실패. 로컬 변경 사항이 있는지 확인하세요: !LOG!
        set "RESULT=pull_failed"
        goto :finish
    )
    echo  [1/2] 갱신 완료.
    set "RESULT=updated"
)

:finish
rem ---- 상태 파일 기록 ^(웹에서 마지막 갱신 시각 표시^) ----
echo  [2/2] 상태 파일 기록 중...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='!REPO!\data'; $f=@(); if(Test-Path $p){$f=@(Get-ChildItem -LiteralPath $p -Filter 'marcap-*' | Sort-Object Name)}; $latest=''; if($f.Count -gt 0){$latest=$f[-1].Name}; $rev=''; if(Test-Path '!REPO!\.git'){$rev=(git -C '!REPO!' rev-parse --short HEAD 2>$null)}; $o=[ordered]@{last_sync=(Get-Date).ToString('yyyy-MM-dd HH:mm:ss');result='!RESULT!';repo_path='marcap';latest_file=$latest;file_count=$f.Count;git_rev=$rev}; ($o|ConvertTo-Json) | Set-Content -LiteralPath '!STATUS!' -Encoding UTF8"

echo  [2/2] 완료: !STATUS!
echo.
type "!STATUS!"
echo.
>>"!LOG!" echo [DONE] result=!RESULT!

if /i "!RESULT!"=="updated" goto :ok
if /i "!RESULT!"=="cloned"  goto :ok
echo  결과: 실패 ^(!RESULT!^)  - 로그: !LOG!
goto :end
:ok
echo  결과: 정상 ^(!RESULT!^)
:end
echo.
if /i "%1"=="/silent" goto :quit
pause
:quit
endlocal
