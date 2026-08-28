Set shell = CreateObject("WScript.Shell")
baseDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd.exe /c cd /d """ & baseDir & """ && ""D:\python\python.exe"" realtime_agent.py"
shell.Run cmd, 0, False
