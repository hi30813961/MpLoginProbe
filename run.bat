@echo off
setlocal
set PATH=C:\c\Qt\6.5.3\mingw_64\bin;C:\Qt\Tools\Tools\mingw1310_64\bin;%PATH%
cd /d "%~dp0"
start "" "MpLoginProbe.exe"
