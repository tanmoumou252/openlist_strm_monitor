Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Environment("PROCESS")("BRIDGE_HEADLESS") = "1"

' Check if python is available
Dim pythonPath
pythonPath = WshShell.ExpandEnvironmentStrings("%PYTHON_EXE%")
If pythonPath = "%PYTHON_EXE%" Then
    pythonPath = "python"
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

' Launch in background with stderr redirected to log file
Dim execCommand
execCommand = "cmd /c " & quotedPythonPath & " src\webui\server.py 2>strm_bridge_boot.log"
WshShell.Run execCommand, 0, False

Set WshShell = Nothing
