@echo off
rem Headless test-suite. Usage: run_tests.cmd [qt5|qt6] [unittest args]
rem Set QGIS_QT5_ROOT / QGIS_QT6_ROOT if QGIS is installed elsewhere.
setlocal
if "%QGIS_QT5_ROOT%"=="" set "QGIS_QT5_ROOT=C:\Program Files\QGIS 3.44.9"
if "%QGIS_QT6_ROOT%"=="" set "QGIS_QT6_ROOT=C:\Program Files\QGISQT6 3.44.4"
set FLAVOUR=%1
if "%FLAVOUR%"=="" set FLAVOUR=qt5
if /I "%FLAVOUR%"=="qt6" (
  for %%f in ("%QGIS_QT6_ROOT%\bin\python-qgis*.bat") do set "PYQGIS=%%f"
) else (
  for %%f in ("%QGIS_QT5_ROOT%\bin\python-qgis*.bat") do set "PYQGIS=%%f"
)
cd /d "%~dp0"
set QT_QPA_PLATFORM=offscreen
call "%PYQGIS%" -W ignore -m unittest discover -s topological_paste\tests -t . -p "test_*.py" %2 %3 %4
endlocal
