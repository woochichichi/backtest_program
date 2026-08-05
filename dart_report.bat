@echo off
setlocal enabledelayedexpansion
title DART 데이터 진단

rem ==================================================================
rem  DART 재무 데이터 진단
rem   - 받아 둔 dart 폴더를 읽어 제대로 받아졌는지 점검합니다
rem   - 인터넷을 쓰지 않습니다. DART 에 접속하지 않습니다
rem   - 결과를 화면에 보여주면서 logs\dart_report.txt 로도 저장합니다
rem ==================================================================

set "ROOTLONG=%~dp0"
set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "VENV=!ROOT!\.venv"
set "PYEXE=!VENV!\Scripts\python.exe"
set "LOGDIR=!ROOT!\logs"
set "REPORT=!LOGDIR!\dart_report.txt"
set "REPORTLONG=!ROOTLONG!logs\dart_report.txt"
set "DARTDIR=!ROOT!\dart"
set "MARCAPDIR=!ROOT!\marcap"

rem ---- 한글이 깨지지 않도록 콘솔과 파이썬 출력을 cp949 로 맞춘다 ----
chcp 949 >nul 2>&1
set "PYTHONIOENCODING=cp949:replace"

if not exist "!LOGDIR!" mkdir "!LOGDIR!"

echo.
echo  ==========================================
echo   DART 데이터 진단
echo  ==========================================
echo.

rem ---- 가상환경 확인 ----
if not exist "!PYEXE!" (
    echo  [오류] 파이썬 가상환경이 없습니다.
    echo         경로 : !VENV!
    echo.
    echo         install.bat 을 먼저 실행해 주세요.
    goto :end
)

rem ---- 데이터 폴더 확인 ----
if not exist "!DARTDIR!" (
    echo  [안내] dart 폴더가 없습니다. 아직 재무 데이터를 받지 않으셨습니다.
    echo         update_dart.bat 을 먼저 실행해 주세요.
    echo.
)

cd /d "!ROOT!"

echo  진단하는 중입니다. 데이터 양에 따라 10 ~ 60초 걸립니다.
echo  인터넷은 쓰지 않습니다.
echo.

rem ---- 진단 실행 ^(화면 대신 파일로 받은 뒤 그대로 뿌린다^) ----
"!PYEXE!" -m tools.fetch_dart --report --out "!DARTDIR!" --marcap "!MARCAPDIR!" > "!REPORT!" 2>&1
set "RC=!errorlevel!"

if not exist "!REPORT!" (
    echo  [오류] 진단 결과 파일을 만들지 못했습니다.
    echo         logs 폴더에 쓸 권한이 있는지 확인해 주세요.
    goto :end
)

type "!REPORT!"

echo.
echo  ------------------------------------------
if "!RC!"=="0" echo   판정 : 정상 - 데이터가 제대로 받아졌습니다
if "!RC!"=="1" echo   판정 : 확인 필요 - 위 목록을 담당자에게 보내주세요
if "!RC!"=="2" echo   판정 : 데이터 없음 - update_dart.bat 을 먼저 실행하세요
echo  ------------------------------------------
echo.
echo  진단 결과를 아래 파일로 저장했습니다.
echo.
echo      !REPORTLONG!
echo.
echo  이 파일을 담당자에게 보내주세요.
echo  메모장으로 열어서 내용을 복사해 붙여 넣으셔도 됩니다.
echo.
echo  폴더를 바로 열려면 이 창을 닫은 뒤 아래를 실행하세요.
echo      explorer "!ROOTLONG!logs"

:end
echo.
pause
endlocal
