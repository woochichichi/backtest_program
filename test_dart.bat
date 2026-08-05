@echo off
setlocal enabledelayedexpansion
title DART 연동 점검

rem ==================================================================
rem  DART 연동 점검 (한 번 눌러 확인하는 용도)
rem   1) 인증키가 있는지
rem   2) DART 에 접속되는지
rem   3) 삼성전자 재무를 실제로 받아 부채비율/유동비율을 계산하는지
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"
set "VENV=!ROOT!\.venv"
set "PYEXE=!VENV!\Scripts\python.exe"
set "KEYFILE=!ROOT!\dart_key.txt"

echo.
echo  ==========================================
echo   DART 연동 점검
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

rem ---- 인증키 위치 미리 알려주기 ----
if not exist "!KEYFILE!" (
    if not defined DART_API_KEY (
        echo  [안내] dart_key.txt 파일도 없고 DART_API_KEY 환경변수도 없습니다.
        echo         아래 1단계에서 발급 방법을 안내합니다.
        echo.
    )
)

cd /d "!ROOT!"

rem ---- 실제 점검 ----
"!PYEXE!" -m tools.fetch_dart --selftest
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
echo   결과 : 정상
echo  ------------------------------------------
echo.
echo   다음 단계 : update_dart.bat 을 실행해 재무 데이터를 받으세요.
echo   처음 한 번은 20 ~ 40분 걸립니다. 중간에 끊겨도 다시 실행하면 이어받습니다.
goto :end

:nokey
echo  ------------------------------------------
echo   결과 : 실패 - 인증키가 없습니다
echo  ------------------------------------------
echo.
echo   1. https://opendart.fss.or.kr 에서 회원가입을 하세요.
echo   2. 오픈API 인증키 신청 메뉴에서 키를 발급받으세요. 보통 바로 나옵니다.
echo   3. 발급받은 40자리 키를 아래 파일에 한 줄로 저장하세요.
echo.
echo        !KEYFILE!
echo.
echo   4. 저장한 뒤 이 창을 닫고 test_dart.bat 을 다시 실행하세요.
echo.
echo   dart_key.txt 는 깃에 올라가지 않도록 이미 제외되어 있습니다.
goto :end

:badkey
echo  ------------------------------------------
echo   결과 : 실패 - 인증키가 잘못되었습니다
echo  ------------------------------------------
echo.
echo   - dart_key.txt 안에 키만 한 줄로 들어 있는지 확인하세요.
echo     따옴표나 앞뒤 공백, 줄바꿈이 섞이면 실패합니다.
echo   - DART 사이트에서 키가 아직 살아 있는지 확인하세요.
echo   - 방금 발급받았다면 몇 분 뒤에 다시 시도해 보세요.
goto :end

:network
echo  ------------------------------------------
echo   결과 : 실패 - DART 에 접속하지 못했습니다
echo  ------------------------------------------
echo.
echo   - 인터넷이 연결되어 있는지 확인하세요.
echo   - 회사 네트워크나 방화벽이 opendart.fss.or.kr 을 막고 있을 수 있습니다.
echo     이 경우 개인 인터넷에서 다시 시도하세요.
echo   - 백신이나 보안 프로그램이 파이썬의 인터넷 접속을 막는 경우도 있습니다.
echo   - 프록시를 쓴다면 HTTPS_PROXY 환경변수를 확인하세요.
goto :end

:limit
echo  ------------------------------------------
echo   결과 : 실패 - 오늘 호출 한도를 다 썼습니다
echo  ------------------------------------------
echo.
echo   DART 는 인증키 하나로 하루 20,000건까지만 부를 수 있습니다.
echo   내일 다시 실행하세요. 받던 데이터는 이어받으므로 처음부터 다시 받지 않습니다.
goto :end

:maint
echo  ------------------------------------------
echo   결과 : 실패 - DART 시스템 점검 중입니다
echo  ------------------------------------------
echo.
echo   DART 쪽 사정이라 기다리는 수밖에 없습니다.
echo   보통 몇 십 분 안에 끝납니다. 잠시 후 다시 실행하세요.
goto :end

:other
echo  ------------------------------------------
echo   결과 : 실패 - 알 수 없는 오류 ^(코드 !RC!^)
echo  ------------------------------------------
echo.
echo   위에 찍힌 메시지를 그대로 복사해 두시면 원인을 찾기 쉽습니다.
goto :end

:end
echo.
pause
endlocal
