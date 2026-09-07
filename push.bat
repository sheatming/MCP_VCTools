@echo off
REM coding-mcp 一键推送脚本
REM 绕过 WorkBuddy sandbox 网络限制，用绝对路径调 git + 显式代理
REM
REM 用法：双击或在 cmd 里跑 `push.bat`

setlocal
set "GIT=C:\Users\gaofei\.workbuddy\binaries\PortableGit\versions\1.2.0\mingw64\bin\git.exe"
set "PROXY=http://127.0.0.1:6789"
set "PROJ=%~dp0"

cd /d "%PROJ%"
echo === coding-mcp 一键推送 ===
echo 当前 commit:
"%GIT%" -C "%PROJ%" log --oneline -3
echo.
echo 正在推送 (走代理 %PROXY%)...
"%GIT%" -C "%PROJ%" -c http.proxy=%PROXY% -c https.proxy=%PROXY% push origin master
set ERR=%ERRORLEVEL%
if %ERR% NEQ 0 (
  echo.
  echo *** 推送失败 (退出码 %ERR%) ***
  echo.
  echo 可能原因：
  echo  1. WorkBuddy 代理 (127.0.0.1:6789) 没启动 —— 检查 WorkBuddy 是不是关了
  echo  2. GitHub 凭据过期 —— 重新在 cmd 跑: git push origin master
  echo     如果弹窗，输入 GitHub PAT (Personal Access Token)
  echo  3. 网络问题 —— 等一会再试
  exit /b %ERR%
)
echo.
echo === 推送成功 ===
"%GIT%" -C "%PROJ%" ls-remote origin master
endlocal
