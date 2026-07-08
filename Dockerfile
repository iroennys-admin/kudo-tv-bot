FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000

CMD python3 -c "
import os, socket as s, threading, sys

# Health check server
PORT = int(os.getenv('PORT', '10000'))
resp = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nOK'

def serve():
    sock = s.socket(s.AF_INET, s.SOCK_STREAM)
    sock.setsockopt(s.SOL_SOCKET, s.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', PORT))
    sock.listen(5)
    while True:
        conn, _ = sock.accept()
        conn.recv(1024)
        conn.sendall(resp)
        conn.close()

threading.Thread(target=serve, daemon=True).start()
print(f'Health check on {PORT}', flush=True)

sys.stdout.flush()
import bot
"