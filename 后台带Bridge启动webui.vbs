Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Environment("PROCESS")("BRIDGE_HEADLESS") = "1"
WshShell.Run "python src\webui\server.py", 0, False
Set WshShell = Nothing
