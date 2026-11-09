#!/bin/bash
# In-container build for win32 (image windows-win32: GE-Proton10-15 and the old Win7 build
# VM's toolchain in $TOOLS: Qt 5.6.3 + MinGW 4.9.2, CMake 3.20.1, GnuWin32 zlib,
# OpenSSL 1.0; plus the image's own dumpbin /dependents and Temurin JDK 8) and win32-5.15.2
# (image windows-win32-qt5.15: the same, plus the VM's Qt 5.15.2 + MinGW 8.1.0 and OpenSSL
# 1.1 with the VC++ 2010 runtime in $TOOLS/QtX, used instead of Qt 5.6.3, MinGW 4.9.2 and
# OpenSSL 1.0). Mirrors the TeamCity Windows builds, under Wine.
#
# Contract (same for every platform, see common.build_command in lib/common.py): source in /src (read-only),
# output in /out: build tree /out/build, install tree /out/install/MultiMC, logs
# /out/*.log; the Wine prefix lives in /out/prefix. Settings, PLATFORM and BUILD_NUMBER
# come from the environment; TOOLS, WINE and PREFIX_TEMPLATE from the image.

set -euo pipefail

export WINEPREFIX=/out/prefix WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="

# Incremental unless CLEAN=1: the build tree and Wine prefix are kept. The install tree
# is always fresh.
rm -rf /out/install
if [ "$CLEAN" = 1 ]; then rm -rf "$WINEPREFIX" /out/build; fi
mkdir -p /out/build /out/install
if [ ! -d "$WINEPREFIX" ]; then
    cp -R "$PREFIX_TEMPLATE" "$WINEPREFIX"   # -R, not -a: the copy must belong to the running user
fi
# Only the drives we need: T: tools, S: source, O: output. Nothing else.
rm -f "$WINEPREFIX/dosdevices/z:" "$WINEPREFIX"/dosdevices/*::
ln -sfn "$TOOLS" "$WINEPREFIX/dosdevices/t:"
ln -sfn /src "$WINEPREFIX/dosdevices/s:"
ln -sfn /out "$WINEPREFIX/dosdevices/o:"

# The image says where its JDK is; images from before 2026-10-30 have the VM's at a fixed path.
JDK=${JDK:-'T:\jdk1.8.0_112'}
case "$PLATFORM" in
    win32)
        MINGW='T:\Qt\Tools\mingw492_32'
        QT='T:\Qt\5.6.3\mingw49_32'
        WINDOWS_VERSION=(5 1)   # Windows XP
        OPENSSL_DLLS=("$TOOLS/OpenSSL-Win32/libeay32.dll" "$TOOLS/OpenSSL-Win32/ssleay32.dll")
        ;;
    win32-5.15.2)
        MINGW='T:\QtX\Tools\mingw810_32'
        QT='T:\QtX\5.15.2\mingw81_32'
        WINDOWS_VERSION=(6 1)   # Windows 7, Qt 5.15's minimum
        # Qt's OpenSSL 1.1 needs the VC++ 2010 runtime, which Windows doesn't have by itself
        OPENSSL_DLLS=("$TOOLS/QtX/Tools/OpenSSL/Win_x86/bin/libcrypto-1_1.dll" "$TOOLS/QtX/Tools/OpenSSL/Win_x86/bin/libssl-1_1.dll"
                      "$TOOLS/QtX/vcredist/x86/msvcr100.dll")
        ;;
    *)
        echo "win32.sh doesn't build $PLATFORM"; exit 1
        ;;
esac
export WINEPATH="$MINGW"'\bin;'"$QT"'\bin;T:\Tools;T:\GnuWin32\bin;'"$JDK"'\bin'
export JAVA_HOME="$JDK"
CMAKE='T:\CMake-3.20.1\bin\cmake.exe'

# The updater's keys floor, put next to the build by common.prepare_build_output (updater 2
# only)
UPDATER_FLOOR=
if [ -f /out/updater-keys-floor.json ]; then UPDATER_FLOOR='O:\updater-keys-floor.json'; fi

# The Windows version our binaries need, in their PE headers (OS and subsystem version), so
# the gate measures it and Windows refuses to start them on anything older. MinGW writes 4.0
# otherwise. The toolchain's DLLs keep theirs.
PE_VERSION="-Wl,--major-os-version,${WINDOWS_VERSION[0]},--minor-os-version,${WINDOWS_VERSION[1]}"
PE_VERSION+=",--major-subsystem-version,${WINDOWS_VERSION[0]},--minor-subsystem-version,${WINDOWS_VERSION[1]}"
# MinGW 8.1's ld puts the link time into the PE header and the export directory; MinGW
# 4.9.2's doesn't (and doesn't know the option).
if [ "$PLATFORM" = win32-5.15.2 ]; then PE_VERSION+=",--no-insert-timestamp"; fi

# Qt 5.15's rcc embeds file modification times in resources (Qt 5.6's doesn't). A fixed
# value (1980-01-01, the zip epoch) makes clean and incremental builds agree, as in osx64.sh.
export SOURCE_DATE_EPOCH=315532800

cd /out/build
$WINE "$CMAKE" -G "MinGW Makefiles" \
    -DCMAKE_MAKE_PROGRAM=mingw32-make \
    -DCMAKE_C_COMPILER=gcc \
    -DCMAKE_CXX_COMPILER=g++ \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
    -DCMAKE_INSTALL_PREFIX:PATH='O:\install\MultiMC' \
    -DCMAKE_EXE_LINKER_FLAGS="$PE_VERSION" \
    -DCMAKE_SHARED_LINKER_FLAGS="$PE_VERSION" \
    -DCMAKE_PREFIX_PATH="$QT" \
    -DQt5_DIR="$QT" \
    -DZLIB_ROOT='T:\GnuWin32' \
    -DLauncher_NOTIFICATION_URL:STRING="$NOTIFICATION_URL" \
    -DLauncher_UPDATER_BASE="$UPDATER_BASE" \
    -DLauncher_TRANSLATIONS_BASE_URL="$TRANSLATIONS_BASE_URL" \
    -DLauncher_FILES_MIRRORS="${FILES_MIRRORS// /;}" \
    -DLauncher_META_MIRRORS="${META_MIRRORS// /;}" \
    -DLauncher_UPDATER_VERSION="$UPDATER_VERSION" \
    -DLauncher_UPDATER_ROOT_KEYS="${UPDATER_ROOT_KEYS// /;}" \
    -DLauncher_UPDATER_KEYS_FLOOR="$UPDATER_FLOOR" \
    -DLauncher_UPDATER_VARIANT="$UPDATER_VARIANT" \
    -DLauncher_LAYOUT=win-bundle \
    -DLauncher_VERSION_BUILD="$BUILD_NUMBER" \
    -DLauncher_BUILD_PLATFORM="$PLATFORM" \
    -DLauncher_SUBREDDIT_URL="$SUBREDDIT_URL" \
    -DLauncher_DISCORD_URL="$DISCORD_URL" \
    -DLauncher_BUG_TRACKER_URL="$BUG_TRACKER_URL" \
    -DCMAKE_GP_CMD_PATHS=T:/Tools \
    -DCMAKE_GP_TOOL=dumpbin \
    $CMAKE_EXTRA_ARGS \
    'S:\' > /out/configure.log 2>&1 || { tail -40 /out/configure.log; exit 1; }

$WINE "$CMAKE" --build . -- -j"${JOBS:-$(nproc)}" > /out/build.log 2>&1 \
    || { grep -m30 -E "error|Error" /out/build.log; tail -20 /out/build.log; exit 1; }
$WINE 'T:\CMake-3.20.1\bin\ctest.exe' > /out/test.log 2>&1 || { tail -30 /out/test.log; echo "TESTS FAILED"; exit 1; }
tail -3 /out/test.log
$WINE "$CMAKE" --install . > /out/install.log 2>&1 || { tail -40 /out/install.log; exit 1; }
cp "${OPENSSL_DLLS[@]}" /out/install/MultiMC/
echo "INSTALL OK"
