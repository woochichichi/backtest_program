' ==================================================================
'  KRX 백테스터 - 이것만 더블클릭하세요
'
'  이 파일은 진행 화면(launcher\launcher.hta)을 띄우기만 합니다.
'  실제 순서는 그 화면에서 눈으로 보면서 진행됩니다.
'
'    1. 프로그램 업데이트   (바뀐 게 있을 때만 받습니다)
'    2. 시세 데이터         (받을 게 있을 때만 받습니다)
'    3. 서버 시작           (검은 창 없이 켭니다)
'    4. 브라우저 열기
'
'  이미 켜져 있으면 다시 열기 / 종료를 고르는 화면이 나옵니다.
' ==================================================================
Option Explicit

Const TITLE = "KRX 백테스터"

Dim Q, fso, sh, root, hta, mshta, cmd
Q = Chr(34)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
hta  = root & "\launcher\launcher.hta"

If Not fso.FileExists(hta) Then
    MsgBox "진행 화면 파일을 찾지 못했습니다." & vbCrLf & vbCrLf & _
           hta & vbCrLf & vbCrLf & _
           "update_program.bat 을 실행해 최신으로 받아 주세요.", _
           vbCritical, TITLE
    WScript.Quit 1
End If

mshta = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\mshta.exe"
If Not fso.FileExists(mshta) Then mshta = "mshta.exe"

' 프로그램 폴더를 인자로 넘긴다 (경로에 한글과 공백이 있어 반드시 따옴표로 감싼다)
cmd = Q & mshta & Q & " " & Q & hta & Q & " " & Q & root & Q

On Error Resume Next
sh.Run cmd, 1, False
If Err.Number <> 0 Then
    Err.Clear
    MsgBox "진행 화면을 띄우지 못했습니다." & vbCrLf & vbCrLf & _
           "run_web.bat 을 더블클릭하면 검은 창으로 실행할 수 있습니다.", _
           vbExclamation, TITLE
End If
On Error GoTo 0
