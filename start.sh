#!/bin/bash
exec > /tmp/startup.log 2>&1
set -x

echo "=== Starting up ==="
echo "PORT=$PORT"

# Health check server in background
python3 <<'PYEOF' &
import os, sys, http.server, socketserver
PORT = int(os.getenv('PORT', '10000'))
print(f'HS: port {PORT}')
sys.stdout.flush()

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/log':
            self.send_response(200)
            body = open('/tmp/startup.log', 'rb').read()
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
    def log_message(self, *a): pass

socketserver.TCPServer(('0.0.0.0', PORT), H).serve_forever()
PYEOF

sleep 2
echo "Starting bot..."
python3 bot.py
echo "EXIT: $?"
