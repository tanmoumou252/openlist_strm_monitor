' 编码说明:本文件为 UTF-16LE 带 BOM(FF FE)。cscript/wscript 在 Windows 下以系统 ANSI/UTF-16 解释脚本，
' 中文 MsgBox 在 GBK 系统的命令宿主下可正常显示。
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Environment("PROCESS")("BRIDGE_HEADLESS") = "1"

' 端口检测——启动前检查监听端口是否已被占用。
' 监听端口从 config.toml 的 [webui].port 读取，文件缺失或解析失败时回退默认 8579。
' 移除 findstr LISTENING 过滤器(非英文系统 netstat 输出可能不包含 LISTENING)，
' 仅判 ":" & webuiPort 有输出即可确定端口已被占用。
Dim webuiPort
webuiPort = GetWebUIPort()
Dim portCheck
portCheck = WshShell.Run("cmd /c netstat -ano | findstr "":" & webuiPort & """ >nul 2>&1", 0, True)
If portCheck = 0 Then
    MsgBox "端口 " & webuiPort & " 已被占用。" & vbCrLf & vbCrLf & _
           "STRM Bridge 可能已在运行中，请先关闭已有实例。" & vbCrLf & _
           "如需强制启动，请先关闭已有进程或修改端口配置。", _
           vbExclamation, "启动检查"
    WScript.Quit 1
End If

' Check if python is available
Dim pythonPath
pythonPath = WshShell.ExpandEnvironmentStrings("%PYTHON_EXE%")
If pythonPath = "%PYTHON_EXE%" Then
    pythonPath = "python"
End If

' 规范化引号——先去除已有外层引号，再统一加一层命令行引号
If Left(pythonPath, 1) = """" And Right(pythonPath, 1) = """" Then
    pythonPath = Mid(pythonPath, 2, Len(pythonPath) - 2)
End If

' 用双引号包裹路径，防止路径含空格时命令断裂
Dim quotedPythonPath
quotedPythonPath = """" & pythonPath & """"

' Test if python command is valid
' 轮询 testExec.Status 实现 ~30 秒超时(每次 100ms)，防止 python --version
' 挂起(Store 弹窗 / 杀软沙箱 / 网络盘不可达等场景)时无限卡死；
' 加 On Error 兜底。不能用 AtEndOfStream 判断——进程静默挂起时它会一直阻塞。
On Error Resume Next
Dim testExec
Set testExec = WshShell.Exec("cmd /c " & quotedPythonPath & " --version")
Dim output
output = ""
Dim pollCount
pollCount = 0
If IsObject(testExec) Then
    ' 等待进程结束(正常退出或超时强制终止)
    Do While testExec.Status = 0
        WScript.Sleep 100
        pollCount = pollCount + 1
        If pollCount >= 300 Then Exit Do
    Loop
    If pollCount >= 300 Then
        ' 已等待约 30 秒仍无输出/未退出，强制终止挂起的进程
        testExec.Terminate
    Else
        ' 进程已结束，读取全部输出
        output = testExec.StdOut.ReadAll
    End If
    testExec.StdOut.Close
End If
On Error GoTo 0

' If python not found or error, show message and exit
If InStr(output, "Python") = 0 Then
    MsgBox "Python 3.11+ is not available or not in PATH." & vbCrLf & vbCrLf & _
           "Please install Python or set PYTHON_EXE environment variable.", _
           vbCritical, "启动错误"
    WScript.Quit 1
End If

' 检查 Python 版本 >= 3.11
' 使用 Run 方法替代 cmd /c + Exec，避免超过 2 个引号时 cmd.exe 剥离首尾引号导致命令失败
Dim exitCode
exitCode = WshShell.Run(quotedPythonPath & " -c ""import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)""", 0, True)
If exitCode <> 0 Then
    MsgBox "Python 版本过低，需要 3.11 或更高版本。" & vbCrLf & vbCrLf & _
           "当前版本: " & output, _
           vbCritical, "启动错误"
    WScript.Quit 1
End If

' Launch in background (隐藏窗口下 stdout/stderr 自然丢弃，不再产生 strm_bridge_boot.log；
' WebUI 启动期日志已由 server.py 在启动时 setup_logging 写入共享 strm_bridge.log)
Dim execCommand
execCommand = "cmd /c " & quotedPythonPath & " """ & WshShell.CurrentDirectory & "\src\webui\server.py"""
WshShell.Run execCommand, 0, False

' 从当前目录 config.toml 读取 [webui].port；文件缺失或解析失败时回退默认 8579。
' 端口检查和错误提示使用同一最终值。
Function GetWebUIPort()
    GetWebUIPort = 8579
    Dim fso, cfgPath, content, f
    Set fso = CreateObject("Scripting.FileSystemObject")
    cfgPath = WshShell.CurrentDirectory & "\config.toml"
    If Not fso.FileExists(cfgPath) Then Exit Function
    Set f = fso.OpenTextFile(cfgPath, 1, False)
    content = f.ReadAll
    f.Close
    Dim secStart
    secStart = InStr(content, "[webui]")
    If secStart = 0 Then Exit Function
    ' 统一换行符为 vbCrLf，兼容 LF-only 文件(Unix 工具编辑后)
    content = Replace(content, vbCr, "")
    content = Replace(content, vbLf, vbCrLf)
    Dim lines, line, low, eqPos, value, i
    lines = Split(Mid(content, secStart), vbCrLf)
    For i = 0 To UBound(lines)
        line = Trim(lines(i))
        If Left(line, 1) = "[" Then
            ' 首行是 [webui] 段头；之后出现 [ 开头行表示已进入下一段
            If i > 0 Then Exit For
        End If
        low = LCase(line)
        If Left(low, 5) = "port " Or Left(low, 5) = "port=" Then
            eqPos = InStr(line, "=")
            If eqPos > 0 Then
                value = Trim(Mid(line, eqPos + 1))
                If InStr(value, "#") > 0 Then value = Trim(Left(value, InStr(value, "#") - 1))
                If InStr(value, ";") > 0 Then value = Trim(Left(value, InStr(value, ";") - 1))
                If IsNumeric(value) Then
                    GetWebUIPort = CInt(value)
                    Exit Function
                End If
            End If
        End If
    Next
End Function

Set WshShell = Nothing
