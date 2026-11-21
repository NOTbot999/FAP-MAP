@echo off
rem Starts the "FrancPhone" Android emulator (Pixel 6, Android 14) and installs the latest Franc Map APK.
rem Needs hardware acceleration: Windows Hypervisor Platform (or the Android Emulator Hypervisor Driver).
setlocal
cd /d "%~dp0"
set "T=%~dp0toolchain"
set "ANDROID_HOME=%T%\sdk"
set "ANDROID_SDK_ROOT=%T%\sdk"
set "ANDROID_AVD_HOME=%T%\avd"
set "ADB=%T%\sdk\platform-tools\adb.exe"

"%T%\sdk\emulator\emulator.exe" -accel-check | findstr /i "WHPX AEHD usable" >nul
if errorlevel 1 (
  echo Hardware acceleration is not available.
  echo Turn on "Windows Hypervisor Platform" in "Turn Windows features on or off" and restart Windows.
  pause
  exit /b 1
)

echo Starting the emulator...
start "FrancPhone" "%T%\sdk\emulator\emulator.exe" -avd FrancPhone -gpu auto -no-snapshot-save
"%ADB%" wait-for-device
echo Waiting for Android to finish booting...
:boot
"%ADB%" shell getprop sys.boot_completed 2>nul | findstr "1" >nul || (timeout /t 3 >nul & goto boot)

for %%F in ("%~dp0..\release\FrancMap-*-Android.apk") do set "APK=%%~fF"
echo Installing %APK% ...
"%ADB%" install -r "%APK%"
"%ADB%" shell am start -n si.francmap.app/.MainActivity
echo Franc Map is running in the emulator.
