"""Serve a files tree over HTTPS, for updater/update-test.py. Runs inside the Launcher's Linux
toolchain image (Python 3.4: keep it plain), as the files site and its mirrors (the test maps
their host names to this container).

    python3 update-test-serve.py <root> <certificate> <key>

Requests are answered in parallel (the Launcher downloads several files at once) and logged
to stderr. Port 443.
"""

import http.server
import socketserver
import ssl
import sys
import os


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("serve: %s\n" % (format % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    root, certificate, key = sys.argv[1:4]
    os.chdir(root)
    server = Server(("0.0.0.0", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
