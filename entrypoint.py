"""Entrypoint for Render - starts health check server then bot."""
import os
import socket as s
import threading
import runpy

PORT = int(os.getenv('PORT', '10000'))
resp = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nOK'


def health_check():
    sock = s.socket(s.AF_INET, s.SOCK_STREAM)
    sock.setsockopt(s.SOL_SOCKET, s.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', PORT))
    sock.listen(5)
    while True:
        conn, _ = sock.accept()
        conn.recv(1024)
        conn.sendall(resp)
        conn.close()


threading.Thread(target=health_check, daemon=True).start()
print(f"Health check on port {PORT}")
runpy.run_path('bot.py', run_name='__main__')
