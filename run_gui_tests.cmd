@echo off
rem GUI integration tests (native clipboard). Usage: run_gui_tests.cmd [qt5|qt6]
rem Starts a real QGIS with a throw-away profile, runs the tests, quits.
rem NOTE: the tests use the system clipboard - its content is replaced.
setlocal
if "%QGIS_QT5_ROOT%"=="" set "QGIS_QT5_ROOT=C:\Program Files\QGIS 3.44.9"
if "%QGIS_QT6_ROOT%"=="" set "QGIS_QT6_ROOT=C:\Program Files\QGISQT6 3.44.4"
set FLAVOUR=%1
if "%FLAVOUR%"=="" set FLAVOUR=qt5
if /I "%FLAVOUR%"=="qt6" (
  set "ROOT=%QGIS_QT6_ROOT%"
  set QT=qt6
) else (
  set "ROOT=%QGIS_QT5_ROOT%"
  set QT=qt5
)
call "%ROOT%\bin\o4w_env.bat"
if /I "%FLAVOUR%"=="qt6" call "%ROOT%\bin\qt6_env.bat"
set APP=
for /d %%d in ("%OSGEO4W_ROOT%\apps\qgis*") do set "APP=%%~nxd"
path %OSGEO4W_ROOT%\apps\%APP%\bin;%PATH%
set QGIS_PREFIX_PATH=%OSGEO4W_ROOT:\=/%/apps/%APP%
set GDAL_FILENAME_IS_UTF8=YES
set QT_PLUGIN_PATH=%OSGEO4W_ROOT%\apps\%APP%\qtplugins;%OSGEO4W_ROOT%\apps\%QT%\plugins
set "TOPOPASTE_TEST_DIR=%~dp0topological_paste\tests"
set "TOPOPASTE_GUI_REPORT=%~dp0gui_report_%FLAVOUR%.txt"
set "PROFILE=%TEMP%\topopaste_gui_profile_%FLAVOUR%"
if exist "%TOPOPASTE_GUI_REPORT%" del "%TOPOPASTE_GUI_REPORT%"
"%OSGEO4W_ROOT%\bin\%APP%-bin.exe" --nologo --noversioncheck --noplugins --profiles-path "%PROFILE%" --code "%TOPOPASTE_TEST_DIR%\gui_integration.py"
type "%TOPOPASTE_GUI_REPORT%"
endlocal
