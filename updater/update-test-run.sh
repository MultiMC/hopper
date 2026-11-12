#!/bin/bash
# One step of updater/update-test.py, inside the platform's toolchain image: serve the files
# tree (/tree) over HTTPS as the files site and its mirrors, then run the Launcher installed in
# /t/MultiMC with --update <tier>, headless, and exit with its status.
#
#     update-test-run.sh <tier>
#
# Mounted: /tree the files tree (read-only), /tls the test CA's server certificate and key,
# /etc/ssl/certs the test CA alone (so nothing else is trusted), /t the installation and the
# server's log, /run the scripts. The image's Qt is in /opt/qt56 (the Linux images; the start
# script takes it from LAUNCHER_LIBRARY_PATH).
set -u

tier=$1

python3 /run/update-test-serve.py /tree /tls/server.pem /tls/server.key 2>>/t/serve.log &
server=$!
# the server listens within a second; the Launcher retries a refused connection on its mirrors anyway
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if (exec 3<>/dev/tcp/127.0.0.1/443) 2>/dev/null; then break; fi
    sleep 0.5
done

export QT_QPA_PLATFORM=offscreen LAUNCHER_LIBRARY_PATH=/opt/qt56/lib
timeout "${STEP_TIMEOUT:-180}" /t/MultiMC/MultiMC --update "$tier"
status=$?
kill "$server" 2>/dev/null
exit "$status"
