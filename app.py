"""Abre o app no seu computador (sem recompensas, exportação até 8K).

Uso: python app.py [--sem-navegador]   (ou dois cliques em "Abrir app.command")
Roda só no seu computador (127.0.0.1); nada é enviado para a internet.
Para o servidor público com recompensas, use o Docker (veja o README).
"""
import os
import socket
import sys
import threading
import webbrowser

RAIZ = os.path.dirname(os.path.abspath(__file__))

# padrões do modo local (podem ser sobrescritos pelo ambiente)
os.environ["LOCAL_MODE"] = "1"
os.environ["COINOS_TOKEN"] = ""  # o app local nunca liga recompensas
os.environ.setdefault("DATA_DIR", os.path.join(RAIZ, "data"))
os.environ.setdefault("MAX_EXPORT_WIDTH", "7680")
os.environ.setdefault("PREVIEW_WIDTH", "1600")
os.environ.setdefault("SAVE_EXPORTS_TO", os.path.join(RAIZ, "output"))
os.environ.setdefault("EXPORTS_PER_10MIN", "1000")


def porta_livre(inicio=8765):
    p = inicio
    while True:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
        p += 1


def main():
    import uvicorn

    porta = porta_livre()
    url = f"http://127.0.0.1:{porta}"
    print(f"App aberto em {url}  (Ctrl+C para fechar)")
    if "--sem-navegador" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run("servidor.main:app", host="127.0.0.1", port=porta, log_level="warning")


if __name__ == "__main__":
    main()
