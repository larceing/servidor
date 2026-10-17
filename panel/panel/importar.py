# -*- coding: utf-8 -*-
"""Una vez, al instalar: el token de GitHub que guardaba el panel de Ajustes
de antes (dentro de Tareas, cifrado con AES-GCM y una clave derivada con
PBKDF2) pasa a este panel. Lo llama servidor.py con
  docker exec -i servidor-panel python -m panel.importar
y por stdin {"cifrado": ..., "clave": ...}. No hace nada si ya hay token."""
import base64
import hashlib
import json
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import guardado


def descifrar_de_antes(clave: str, valor_b64: str) -> str:
    blob = base64.b64decode(valor_b64)
    sal, nonce, resto = blob[:16], blob[16:28], blob[28:]
    k = hashlib.pbkdf2_hmac("sha256", clave.encode(), sal, 200_000, dklen=32)
    return AESGCM(k).decrypt(nonce, resto, None).decode()


def main() -> int:
    if guardado.hay_token():
        print("ya había token")
        return 0
    datos = json.load(sys.stdin)
    guardado.guardar_token(descifrar_de_antes(datos["clave"], datos["cifrado"]))
    print("token importado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
