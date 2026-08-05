@echo off
setlocal enabledelayedexpansion
title 바탕화면 바로가기 만들기

rem ==================================================================
rem  바탕화면에 "KRX 백테스터" 아이콘을 만듭니다.
rem  - 한 번만 실행하면 됩니다.
rem  - 아이콘을 지우고 싶으면 바탕화면에서 그냥 삭제하세요.
rem    (프로그램은 지워지지 않습니다)
rem ==================================================================

set "ROOT=%~dp0"
for %%d in ("!ROOT!.") do set "ROOT=%%~sd"

echo.
echo  ==========================================
echo   바탕화면 바로가기 만들기
echo  ==========================================
echo.

if not exist "!ROOT!\launcher\launcher.hta" (
    echo  [안내] launcher 폴더가 보이지 않습니다.
    rem  바로가기 자체는 만들 수 있으므로 계속 진행한다.
    echo         나중에 update_program.bat 을 한 번 실행해 주세요.
    echo.
)

set "PSOUT="
for /f "delims=" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $r=(Get-Item -LiteralPath '!ROOT!').FullName.TrimEnd('\'); $vbs=Get-ChildItem -LiteralPath $r -Filter '*.vbs' ^| Select-Object -First 1; if (-not $vbs) { throw 'vbs not found' }; $nm='KRX ' + (-join ([int[]](0xBC31,0xD14C,0xC2A4,0xD130) ^| ForEach-Object { [char]$_ })); $dt=[Environment]::GetFolderPath('Desktop'); $lnk=Join-Path $dt ($nm + '.lnk'); $ico=Join-Path $r 'assets\krx.ico'; $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($lnk); $s.TargetPath=$vbs.FullName; $s.WorkingDirectory=$r; $s.Description=$nm; if (Test-Path -LiteralPath $ico) { $s.IconLocation=$ico + ',0' }; $s.Save(); Write-Output ('OK|' + $lnk); exit 0 } catch { Write-Output ('ERR|' + $_.Exception.Message); exit 1 }"') do set "PSOUT=%%a"

if not defined PSOUT goto :failed

set "HEAD=!PSOUT:~0,2!"
if /i not "!HEAD!"=="OK" goto :failed

echo  바탕화면에 아이콘을 만들었습니다.
echo.
echo  만든 곳 : !PSOUT:~3!
echo.
echo  이제 바탕화면의 "KRX 백테스터" 아이콘을 더블클릭하면
echo  프로그램 업데이트, 시세 갱신, 실행이 한 번에 진행됩니다.
goto :end

:failed
echo  [오류] 바로가기를 만들지 못했습니다.
echo.
if defined PSOUT echo         이유 : !PSOUT:~4!
echo         이 폴더의 KRX백테스터.vbs 를 마우스 오른쪽 클릭한 뒤
echo         [보내기] - [바탕 화면에 바로 가기 만들기] 를 눌러도 됩니다.

:end
echo.
pause
endlocal
