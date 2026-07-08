"""Minimal entrypoint that starts health check + bot."""
import os
import socket as s
import threading

# Start health check immediately
PORT = int(os.getenv('PORT', '10000'))
resp = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nOK'

def health():
    sock = s.socket(s.AF_INET, s.SOCK_STREAM)
    sock.setsockopt(s.SOL_SOCKET, s.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', PORT))
    sock.listen(5)
    while True:
        conn, _ = sock.accept()
        conn.recv(1024)
        conn.sendall(resp)
        conn.close()

t = threading.Thread(target=health, daemon=True)
t.start()
print(f'Health check on :{PORT}', flush=True)

# Start bot
import bot