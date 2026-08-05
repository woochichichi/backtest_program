@echo off
setlocal enabledelayedexpansion
title DART 재무 데이터 갱신

rem ==================================================================
rem  DART 재무 데이터 갱신
rem   - 이미 받은 분기는 자동으로 건너뜁니다
rem   - 최근 2개 분기만 정정공시 확인용으로 다시 받습니다
rem   - 중간에 끊겨도 다시 실행하면 이어받습니다
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "VENV=!ROOT!\.venv"
set "PYEXE=!VENV!\Scripts\python.exe"
set "OUTDIR=!ROOT!\dart"
set "KEYFILE=!ROOT!\dart_key.txt"
set "FROMYEAR=2015"

echo.
echo  ==========================================
echo   DART 재무 데이터 갱신
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

rem ---- 인증키 확인 ----
if not exist "!KEYFILE!" (
    if not defined DART_API_KEY (
        echo  [오류] DART 인증키를 찾을 수 없습니다.
        echo.
        echo         test_dart.bat 을 먼저 실행하면 발급 방법을 안내합니다.
        goto :end
    )
)

cd /d "!ROOT!"

rem ---- 안내 ----
if exist "!OUTDIR!\.fetch_state.json" (
    echo  이미 받아 둔 데이터가 있습니다. 새로 나온 분기만 받습니다.
    echo  보통 1 ~ 3분이면 끝납니다.
) else (
    echo  처음 받는 중입니다. !FROMYEAR!년부터 올해까지 전부 받습니다.
    echo  20 ~ 40분 걸립니다. 창을 닫지 말고 기다려 주세요.
)
echo.
echo  - 이미 받은 분기는 자동으로 건너뜁니다. 같은 자료를 두 번 받지 않습니다.
echo  - 가장 최근 2개 분기는 정정공시 때문에 매번 다시 확인합니다.
echo  - 아직 공시 기간이 아닌 분기는 아예 요청하지 않습니다.
echo  - 중간에 끊겨도 다시 실행하면 이어받습니다.
echo  - DART 는 하루 20,000건까지만 부를 수 있습니다. 넘으면 알아서 멈춥니다.
echo.
echo  저장 위치 : !OUTDIR!
echo.
echo  ------------------------------------------
echo.

rem ---- 실행 ----
"!PYEXE!" -m tools.fetch_dart --from !FROMYEAR! --out "!OUTDIR!"
set "RC=!errorlevel!"

echo.
if "!RC!"=="0" goto :ok
if "!RC!"=="3" goto :nokey
if "!RC!"=="4" goto :badkey
if "!RC!"=="5" goto :network
if "!RC!"=="6" goto :limit
if "!RC!"=="7" goto :maint
goto :other

:ok
echo  ------------------------------------------
echo   결과 : 정상 완료
echo  ------------------------------------------
echo.
echo   이제 웹 화면에서 재무 조건 ^(부채비율, 유동비율, 연속 흑자^) 을 쓸 수 있습니다.
echo   다음에 또 실행하면 새로 나온 분기만 받습니다.
goto :end

:nokey
echo  결과 : 실패 - 인증키가 없습니다. test_dart.bat 을 먼저 실행하세요.
goto :end

:badkey
echo  결과 : 실패 - 인증키가 잘못되었습니다. test_dart.bat 으로 확인하세요.
goto :end

:network
echo  결과 : 실패 - 네트워크 오류입니다.
echo         잠시 후 update_dart.bat 을 다시 실행하면 이어받습니다.
goto :end

:limit
echo  ------------------------------------------
echo   결과 : 오늘 호출 한도를 다 썼습니다
echo  ------------------------------------------
echo.
echo   DART 는 하루 20,000건까지만 부를 수 있습니다.
echo   내일 update_dart.bat 을 다시 실행하면 멈춘 곳부터 이어받습니다.
echo   지금까지 받은 데이터는 그대로 저장되어 있습니다.
goto :end

:maint
echo  결과 : DART 시스템 점검 중입니다. 잠시 후 다시 실행하세요.
goto :end

:other
echo  결과 : 실패 ^(코드 !RC!^)
echo         위에 찍힌 메시지를 확인하세요.
goto :end

:end
echo.
echo  참고 : 받은 것까지 무시하고 처음부터 전부 다시 받으려면 아래를 실행하세요.
echo         .venv\Scripts\python.exe -m tools.fetch_dart --from !FROMYEAR! --force
echo.
if /i "%1"=="/silent" goto :quit
pause
:quit
endlocal
