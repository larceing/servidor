# -*- coding: utf-8 -*-
"""Lo poco que guarda el panel: el token de GitHub, cifrado con PANEL_CLAVE,
en /datos/ajustes.json (un volumen solo del panel)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _dir() -> Path:
    return Path(os.environ.get("DATOS_DIR", "/datos"))


def _fernet() -> Fernet:
    clave = os.environ.get("PANEL_CLAVE", "")
    if not clave:
        raise RuntimeError("falta PANEL_CLAVE")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(clave.encode()).digest()))


def _leer() -> dict:
    try:
        return json.loads((_dir() / "ajustes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _escribir(datos: dict) -> None:
    p = _dir() / "ajustes.json"
    tmp = p.with_name(".ajustes.json.tmp")
    tmp.write_text(json.dumps(datos), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def guardar_token(token: str) -> None:
    _escribir({**_leer(), "token": _fernet().encrypt(token.encode()).decode()})


def token() -> str:
    cifrado = _leer().get("token", "")
    if not cifrado:
        return ""
    try:
        return _fernet().decrypt(cifrado.encode()).decode()
    except InvalidToken:
        return ""


def hay_token() -> bool:
    return bool(_leer().get("token"))
