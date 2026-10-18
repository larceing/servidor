# -*- coding: utf-8 -*-
"""Claves de la API (/ajustes/api/v1/): para que un programa (p. ej. Claude)
pueda ver las apps y desplegar sin la sesion de nadie en el navegador.

Cada clave tiene un nombre, los entornos en que puede desplegar (por defecto
solo DEV), las apps que puede tocar (vacio = todas) y una fecha de caducidad.
Se ensena una sola vez al crearla: aqui solo se guarda su sha256.

/datos/api.json: {"claves": [{id, nombre, hash, entornos, apps, creada, quien, caduca, usada}]}
/datos/api.log:  una linea JSON por cada llamada que cambia algo (o que se rechaza)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

PREFIJO = "srv_"
CLAVE_RE = re.compile(r"^srv_([0-9a-f]{8})_([A-Za-z0-9_-]{30,80})$")
NOMBRE_RE = re.compile(r"^[\w .:@()-]{1,60}$")
_cerrojo = threading.Lock()


def _dir() -> Path:
    return Path(os.environ.get("DATOS_DIR", "/datos"))


def ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(secreto: str) -> str:
    return hashlib.sha256(secreto.encode()).hexdigest()


def _leer() -> list:
    try:
        d = json.loads((_dir() / "api.json").read_text(encoding="utf-8"))
        return [c for c in d.get("claves", []) if isinstance(c, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def _escribir(claves: list) -> None:
    p = _dir() / "api.json"
    tmp = p.with_name(".api.json.tmp")
    tmp.write_text(json.dumps({"claves": claves}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def caducada(c: dict) -> bool:
    return bool(c.get("caduca")) and c["caduca"] <= ahora()


def publica(c: dict) -> dict:
    """Lo que se puede ensenar de una clave (sin su hash)."""
    return {k: c.get(k, "") for k in ("id", "nombre", "entornos", "apps", "creada", "quien", "caduca", "usada")} | {
        "caducada": caducada(c)}


def listar() -> list:
    return [publica(c) for c in _leer()]


def crear(nombre: str, entornos: list, apps: list, quien: str, dias: int = 90) -> tuple:
    """(clave en claro, sus datos). La clave en claro no se guarda en ningun sitio."""
    nombre = nombre.strip()
    if not NOMBRE_RE.match(nombre):
        raise ValueError("El nombre va en letras, números, espacios y . : @ ( ) - (hasta 60).")
    entornos = [e for e in ("dev", "pro") if e in entornos]
    if not entornos:
        raise ValueError("Elige al menos un entorno.")
    cid, secreto = secrets.token_hex(4), secrets.token_urlsafe(32)
    c = {"id": cid, "nombre": nombre, "hash": _hash(secreto), "entornos": entornos, "apps": sorted(set(apps)),
         "creada": ahora(), "quien": quien, "usada": "",
         "caduca": (datetime.now(timezone.utc) + timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%SZ") if dias > 0 else ""}
    with _cerrojo:
        _escribir([x for x in _leer() if x.get("id") != cid] + [c])
    return f"{PREFIJO}{cid}_{secreto}", publica(c)


def revocar(cid: str) -> bool:
    with _cerrojo:
        claves = _leer()
        quedan = [c for c in claves if c.get("id") != cid]
        if len(quedan) == len(claves):
            return False
        _escribir(quedan)
        return True


def comprobar(texto: str) -> dict | None:
    """Los datos de la clave si vale (existe, coincide y no ha caducado); si no, None."""
    m = CLAVE_RE.match(texto or "")
    if not m:
        return None
    cid, secreto = m.groups()
    with _cerrojo:
        claves = _leer()
        c = next((x for x in claves if x.get("id") == cid), None)
        if not c or not secrets.compare_digest(c.get("hash", ""), _hash(secreto)) or caducada(c):
            return None
        # "usada" con precision de minuto: no se reescribe el fichero en cada llamada
        if c.get("usada", "")[:16] != ahora()[:16]:
            c["usada"] = ahora()
            _escribir(claves)
    return c


def puede(c: dict, app_id: str, entorno: str) -> bool:
    return entorno in (c.get("entornos") or []) and (not c.get("apps") or app_id in c["apps"])


def apuntar(c: dict | None, accion: str, datos: dict, ok: bool, detalle: str = "", ip: str = "") -> None:
    """Registro de lo que se ha pedido por la API (lo ve IT en Ajustes)."""
    linea = {"fecha": ahora(), "clave": (c or {}).get("nombre", ""), "clave_id": (c or {}).get("id", ""),
             "accion": accion, "datos": datos, "ok": ok, "detalle": detalle[:300], "ip": ip}
    p = _dir() / "api.log"
    with _cerrojo:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")
        if p.stat().st_size > 1_000_000:   # se queda con lo ultimo
            lineas = p.read_text(encoding="utf-8").splitlines()[-2000:]
            tmp = p.with_name(".api.log.tmp")
            tmp.write_text("\n".join(lineas) + "\n", encoding="utf-8")
            os.replace(tmp, p)


def registro(limite: int = 20) -> list:
    """Lo ultimo apuntado, lo mas nuevo primero."""
    try:
        lineas = (_dir() / "api.log").read_text(encoding="utf-8").splitlines()[-limite:]
    except OSError:
        return []
    salida = []
    for x in reversed(lineas):
        try:
            salida.append(json.loads(x))
        except ValueError:
            pass
    return salida
