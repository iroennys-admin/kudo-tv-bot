#!/bin/bash
# Start health check server in background
python3 -c "
import http.server, socketserver, os
PORT = int(os.getenv('PORT', '10000'))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *a): pass
socketserver.TCPServer(('0.0.0.0', PORT), H).serve_forever()
" &

sleep 1

# Start the bot
python3 bot.py
