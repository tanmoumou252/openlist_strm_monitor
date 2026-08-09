Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Environment("PROCESS")("BRIDGE_HEADLESS") = "1"

' Check if python is available
Dim pythonPath
pythonPath = WshShell.ExpandEnvironmentStrings("%PYTHON_EXE%")
If pythonPath = "%PYTHON_EXE%" Then
    pythonPath = "python"
End If

' P8: 规范化引号——先去除已有外层引号，再统一加一层命令行引号
If Left(pythonPath, 1) = """" And Right(pythonPath, 1) = """" Then
    pythonPath = Mid(pythonPath, 2, Len(pythonPath) - 2)
End If

' H-9: 用双引号包裹路径，防止路径含空格时命令断裂
Dim quotedPythonPath
quotedPythonPath = """" & pythonPath & """"

' Test if python command is valid
Dim testExec
Set testExec = WshShell.Exec("cmd /c " & quotedPythonPath & " --version")
Dim output
output = ""
Do While Not testExec.StdOut.AtEndOfStream
    output = output & testExec.StdOut.ReadLine
Loop
testExec.StdOut.Close

' If python not found or error, show message and exit
If InStr(output, "Python") = 0 Then
    MsgBox "Python 3.11+ is not available or not in PATH." & vbCrLf & vbCrLf & _
           "Please install Python or set PYTHON_EXE environment variable.", _
           vbCritical, "启动错误"
    WScript.Quit 1
End If

' B3: 检查 Python 版本 >= 3.11
' [已修复] 使用 Run 方法替代 cmd /c + Exec，避免超过 2 个引号时 cmd.exe 剥离首尾引号导致命令失败
Dim exitCode
exitCode = WshShell.Run(quotedPythonPath & " -c ""import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)""", 0, True)
If exitCode <> 0 Then
    MsgBox "Python 版本过低，需要 3.11 或更高版本。" & vbCrLf & vbCrLf & _
           "当前版本: " & output, _
           vbCritical, "启动错误"
    WScript.Quit 1
End If

' Launch in background with stderr redirected to log file
Dim execCommand
execCommand = "cmd /c " & quotedPythonPath & " src\webui\server.py 2>strm_bridge_boot.log"
WshShell.Run execCommand, 0, False

Set WshShell = Nothing
