' Запуск LearnQuest в фоне без окна консоли (используется автозапуском).
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Environment("Process")("NO_BROWSER") = "1"
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\run.py""", 0, False
