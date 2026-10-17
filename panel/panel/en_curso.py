# -*- coding: utf-8 -*-
"""Despliegues pedidos que aun no han terminado, guardados en el panel (no en
la sesion): asi cualquier pestana o persona que abra Ajustes ve lo mismo y no
puede pedir otro encima. Uno por app como mucho.

/datos/en_curso.json: {ruta: {entorno, version, desde, quien}}"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

CADUCA = timedelta(minutes=45)   # si en ese tiempo no se sabe nada, se olvida
_cerrojo = threading.Lock()


def _fichero() -> Path:
    return Path(os.environ.get("DATOS_DIR", "/datos")) / "en_curso.json"


def ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leer() -> dict:
    try:
        d = json.loads(_fichero().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _escribir(d: dict) -> None:
    p = _fichero()
    tmp = p.with_name(".en_curso.json.tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, p)


def reservar(ruta: str, entorno: str, version: str, quien: str) -> dict | None:
    """Apunta el despliegue; si ya hay uno de esa app sin terminar, no apunta
    nada y devuelve ese."""
    with _cerrojo:
        d = leer()
        if ruta in d and not caducado(d[ruta]) and not d[ruta].get("fallo"):
            return d[ruta]
        d[ruta] = {"entorno": entorno, "version": version, "desde": ahora(), "quien": quien}
        _escribir(d)
        return None


def soltar(ruta: str) -> None:
    with _cerrojo:
        d = leer()
        if d.pop(ruta, None) is not None:
            _escribir(d)


def caducado(e: dict) -> bool:
    try:
        desde = datetime.strptime(e.get("desde", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - desde > CADUCA


def resolver(ruta: str, tipo: str, version_viva: str, ejecuciones: list, resultados: list) -> dict | None:
    """Lo que sigue en curso de esa app (o None, y lo quita si ya acabo).
    Acaba cuando: el entorno ya dice esa version; o su ejecucion de GitHub
    (la primera a mano desde que se pidio) termino; o el agente respondio a
    la peticion; o caduco. Si fallo, se queda apuntado con 'fallo' (lo ve
    todo el que abra Ajustes) hasta el siguiente despliegue o que caduque."""
    e = leer().get(ruta)
    if not e:
        return None
    if e.get("fallo"):
        if caducado(e):
            soltar(ruta)
            return None
        return e
    fin, fallo, url = False, "", ""
    if caducado(e):
        fin = True
    elif e["version"] != "anterior" and version_viva == e["version"]:
        fin = True
    elif tipo == "workflow":
        mias = [r for r in ejecuciones if r.get("evento") == "workflow_dispatch" and (r.get("creada") or "") >= _antes(e["desde"])]
        if mias:
            r = min(mias, key=lambda x: x.get("creada") or "")
            url = r.get("url", "")
            e = {**e, "estado": r.get("estado", ""), "url": url}
            if r.get("estado") == "completed":
                fin = True
                if r.get("resultado") != "success":
                    fallo = r.get("resultado") or "falló"
    elif tipo == "servidor":
        for r in resultados:
            if r.get("accion") == "desplegar" and (r.get("fin") or "") >= e["desde"]:
                fin = True
                if not r.get("ok"):
                    fallo = "falló"
                break
    if fin and fallo:
        e = {**e, "fallo": fallo, "url": url}
        with _cerrojo:
            d = leer()
            d[ruta] = e
            _escribir(d)
        return e
    if fin:
        soltar(ruta)
        return None
    return e


def _antes(iso: str, segundos: int = 60) -> str:
    """GitHub y el panel no tienen el mismo reloj: un minuto de margen."""
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ") - timedelta(seconds=segundos)
    except ValueError:
        return iso
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")
