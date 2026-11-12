@echo off
setlocal
set QT=C:\c\Qt\6.5.3\mingw_64
set MG=C:\Qt\Tools\Tools\mingw1310_64\bin
set PATH=%QT%\bin;%MG%;%PATH%
cd /d "%~dp0"
g++ -std=c++17 -O2 -mwindows -pthread -finput-charset=UTF-8 -fexec-charset=UTF-8 -DUNICODE -D_UNICODE -DQT_CORE_LIB -DQT_GUI_LIB -DQT_WIDGETS_LIB -DQT_NETWORK_LIB ^
 -I"%QT%\include" -I"%QT%\include\QtCore" -I"%QT%\include\QtGui" -I"%QT%\include\QtWidgets" -I"%QT%\include\QtNetwork" ^
 main.cpp -o MpLoginProbe.exe ^
 -L"%QT%\lib" -lQt6Widgets -lQt6Gui -lQt6Network -lQt6Core -lole32 -loleaut32 -luuid -luser32 -lshlwapi
if errorlevel 1 (echo BUILD FAILED & pause) else (echo BUILD OK)
