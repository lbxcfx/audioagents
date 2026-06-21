@echo off
setlocal
cd /d "%~dp0\.."

where java >nul 2>nul
if errorlevel 1 (
  echo Java is not available on Windows PATH.
  echo Install a Windows JDK/JRE 8 or 11, then run this script again.
  pause
  exit /b 1
)

set "CP=%CD%\callbot-core\target\classes;%CD%\callbot-core\libs\*"
java -cp "%CP%" com.mdx.yyzs.tools.PeersMiniGui

endlocal
