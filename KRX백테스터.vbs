' ==================================================================
'  KRX 백테스터 - 시작 / 종료 (하나로 토글)
'
'  이 파일을 더블클릭하면:
'    - 꺼져 있으면  -> 서버를 보이지 않게 켜고 브라우저를 엽니다
'    - 켜져 있으면  -> 종료할지 물어봅니다
'
'  검은 콘솔 창은 뜨지 않습니다. 서버 로그는 logs\server.log 에 쌓입니다.
' ==================================================================
Option Explicit

Const PORT = 8000
Const TITLE = "KRX 백테스터"

Dim Q, fso, sh, root, venvPy, pidFile, logDir, logFile, url
Q = Chr(34)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
root    = fso.GetParentFolderName(WScript.ScriptFullName)
venvPy  = root & "\.venv\Scripts\python.exe"
pidFile = root & "\.server.pid"
logDir  = root & "\logs"
logFile = logDir & "\server.log"
url     = "http://127.0.0.1:" & PORT & "/"

' ---------------- 이미 켜져 있으면 : 종료 여부를 묻는다 ----------------
If IsRunning() Then
    Dim ans
    ans = MsgBox(TITLE & " 가 이미 켜져 있습니다." & vbCrLf & vbCrLf & _
                 "[예]     서버를 종료합니다" & vbCrLf & _
                 "[아니오] 브라우저 창만 다시 엽니다", _
                 vbYesNoCancel + vbQuestion, TITLE)
    If ans = vbYes Then
        StopServer
        MsgBox "종료했습니다.", vbInformation, TITLE
    ElseIf ans = vbNo Then
        sh.Run url, 1, False
    End If
    WScript.Quit
End If

' ---------------- 설치 확인 ----------------
If Not fso.FileExists(venvPy) Then
    MsgBox "설치가 아직 끝나지 않았습니다." & vbCrLf & vbCrLf & _
           "install.bat 을 먼저 실행하세요." & vbCrLf & _
           "(Python 3.10 ~ 3.13 이 필요합니다)", vbExclamation, TITLE
    WScript.Quit
End If

If Not fso.FolderExists(root & "\marcap") Then
    MsgBox "주가 데이터가 없습니다." & vbCrLf & vbCrLf & _
           "update_marcap.bat 을 먼저 실행해 데이터를 받으세요." & vbCrLf & _
           "(약 1.8GB, 10~30분 걸립니다)", vbExclamation, TITLE
    WScript.Quit
End If

' ---------------- 시작 ----------------
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
StartServer

Dim i, ok
ok = False
For i = 1 To 120           ' 최대 60초 대기
    WScript.Sleep 500
    If IsRunning() Then
        ok = True
        Exit For
    End If
Next

If ok Then
    sh.Run url, 1, False
Else
    MsgBox "서버가 시작되지 않았습니다." & vbCrLf & vbCrLf & _
           "logs\server.log 파일에 원인이 적혀 있습니다." & vbCrLf & _
           "run_web.bat 을 실행하면 화면에서 바로 볼 수도 있습니다.", _
           vbCritical, TITLE
End If

' ==================================================================
'  함수
' ==================================================================

' 서버가 응답하는지 확인한다
Function IsRunning()
    Dim http
    IsRunning = False
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    If Err.Number <> 0 Then
        Err.Clear
        Exit Function
    End If
    http.setTimeouts 1000, 1000, 2000, 3000
    http.open "GET", "http://127.0.0.1:" & PORT & "/api/status", False
    http.send
    If Err.Number = 0 Then
        If http.status = 200 Then IsRunning = True
    End If
    Err.Clear
    On Error GoTo 0
End Function

' 콘솔 창 없이 서버를 띄운다 (ShowWindow = 0 = SW_HIDE)
Sub StartServer()
    Dim svc, proc, cfg, startup, cmd, pid, ret, f
    cmd = "cmd /c " & Q & Q & venvPy & Q & " -m uvicorn server.app:app" & _
          " --host 127.0.0.1 --port " & PORT & " >> " & Q & logFile & Q & " 2>&1" & Q
    On Error Resume Next
    Set svc     = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
    Set proc    = svc.Get("Win32_Process")
    Set cfg     = svc.Get("Win32_ProcessStartup")
    Set startup = cfg.SpawnInstance_
    startup.ShowWindow = 0
    ret = proc.Create(cmd, root, startup, pid)
    If Err.Number = 0 And ret = 0 Then
        Set f = fso.CreateTextFile(pidFile, True)
        f.WriteLine pid
        f.Close
    Else
        Err.Clear
        ' WMI 가 막힌 환경이면 창을 숨긴 채로 대체 실행
        sh.Run cmd, 0, False
    End If
    On Error GoTo 0
End Sub

' 서버와 그 자식 프로세스를 모두 종료한다
Sub StopServer()
    Dim f, pid
    On Error Resume Next
    If fso.FileExists(pidFile) Then
        Set f = fso.OpenTextFile(pidFile, 1)
        pid = Trim(f.ReadLine)
        f.Close
        If IsNumeric(pid) Then sh.Run "taskkill /PID " & pid & " /T /F", 0, True
        fso.DeleteFile pidFile, True
    End If
    Err.Clear
    KillOurPython
    On Error GoTo 0
End Sub

' 이 폴더의 .venv 파이썬만 골라서 정리한다 (다른 파이썬 프로그램은 건드리지 않음)
Sub KillOurPython()
    Dim svc, list, p, target
    target = LCase(venvPy)
    On Error Resume Next
    Set svc  = GetObject("winmgmts:\\.\root\cimv2")
    Set list = svc.ExecQuery("SELECT ProcessId, ExecutablePath FROM Win32_Process " & _
                             "WHERE Name = 'python.exe' OR Name = 'pythonw.exe'")
    For Each p In list
        If LCase(p.ExecutablePath & "") = target Then p.Terminate()
    Next
    Err.Clear
    On Error GoTo 0
End Sub
