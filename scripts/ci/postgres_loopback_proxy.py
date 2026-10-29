"""CI transport only: reach the disposable server's actual loopback listener.

V4 isolation verifies inet_server_addr(), not only a client-side forwarded URL.
The relay has one fixed destination in the shared PostgreSQL network namespace.
It does not relax application target guards or provide a general network proxy.
"""
import select
import socket
import threading


def forward(client):
    try:
        with client, socket.create_connection(('127.0.0.1', 5432), timeout=10) as server:
            server.settimeout(None)
            peers = {client: server, server: client}
            while True:
                readable, _, _ = select.select(tuple(peers), (), ())
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)
    except OSError:
        pass  # The database/client owns protocol errors and disconnect reporting.


if __name__ == '__main__':
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('0.0.0.0', 5433))
        listener.listen(128)
        while True:
            client, _ = listener.accept()
            threading.Thread(target=forward, args=(client,), daemon=True).start()
