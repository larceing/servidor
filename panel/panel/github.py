# -*- coding: utf-8 -*-
"""GitHub: leer el manifiesto de una app, lanzar su workflow, ver sus
ejecuciones y aprobar (o rechazar) las que esperan para PRO. Un solo token
(guardado.py) para todas las apps."""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import guardado

API = "https://api.github.com"
log = logging.getLogger("panel.github")


class GitHubError(Exception):
    pass


def llamar(metodo: str, camino: str, **kw):
    tok = guardado.token()
    if not tok:
        raise GitHubError("Falta el token de GitHub (abajo, en Conexión con GitHub).")
    try:
        r = httpx.request(metodo, API + camino, timeout=15, headers={
            "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}, **kw)
    except httpx.HTTPError as e:
        raise GitHubError(f"No se puede hablar con GitHub: {e}") from e
    if r.status_code in (401, 403):
        raise GitHubError("GitHub no acepta el token: mira que no haya caducado y que vea ese repositorio "
                          "(permisos Actions, Contents y Deployments).")
    if r.status_code == 404:
        raise GitHubError("GitHub no encuentra eso (¿repositorio, rama o workflow bien escritos? ¿el token ve ese repo?).")
    if r.status_code >= 400:
        try:
            detalle = r.json().get("message", "")
        except ValueError:
            detalle = r.text[:200]
        raise GitHubError(f"GitHub respondió {r.status_code}: {detalle}")
    return r.json() if r.content else None


def leer_manifiesto(repo: str, rama: str = "") -> list[dict]:
    """servidor.json de la raiz del repositorio: una app o {"apps": [...]}."""
    ref = f"?ref={rama}" if rama else ""
    datos = llamar("GET", f"/repos/{repo}/contents/servidor.json{ref}")
    try:
        contenido = json.loads(base64.b64decode(datos["content"]).decode("utf-8"))
    except (KeyError, TypeError, ValueError) as e:
        raise GitHubError("servidor.json no es un JSON válido") from e
    apps = contenido.get("apps") if isinstance(contenido, dict) and "apps" in contenido else [contenido]
    if not isinstance(apps, list) or not apps or not all(isinstance(a, dict) for a in apps):
        raise GitHubError("servidor.json: una app (objeto) o {\"apps\": [...]}")
    return apps


def lanzar(d: dict, entorno: str, version: str) -> None:
    llamar("POST", f"/repos/{d['repo']}/actions/workflows/{d['workflow']}/dispatches", json={
        "ref": d["rama"], "inputs": {d["campo_entorno"]: entorno, d["campo_version"]: version}})


def ejecuciones(d: dict, limite: int = 5) -> list[dict]:
    datos = llamar("GET", f"/repos/{d['repo']}/actions/workflows/{d['workflow']}/runs?per_page={limite}") or {}
    return [{"id": r["id"], "titulo": r.get("display_title") or r.get("name", ""), "estado": r.get("status", ""),
             "resultado": r.get("conclusion") or "", "url": r.get("html_url", ""),
             "fecha": (r.get("created_at") or "").replace("T", " ")[:16]} for r in datos.get("workflow_runs", [])]


def pendientes(d: dict) -> list[dict]:
    datos = llamar("GET", f"/repos/{d['repo']}/actions/workflows/{d['workflow']}/runs?status=waiting&per_page=5") or {}
    salida = []
    for r in datos.get("workflow_runs", []):
        for p in llamar("GET", f"/repos/{d['repo']}/actions/runs/{r['id']}/pending_deployments") or []:
            salida.append({"run_id": r["id"], "titulo": r.get("display_title") or r.get("name", ""),
                           "url": r.get("html_url", ""), "entorno": p["environment"]["name"],
                           "entorno_id": p["environment"]["id"], "puede": p.get("current_user_can_approve", False)})
    return salida


def revisar(repo: str, run_id: int, entorno_id: int, aprobar: bool) -> None:
    llamar("POST", f"/repos/{repo}/actions/runs/{int(run_id)}/pending_deployments", json={
        "environment_ids": [int(entorno_id)], "state": "approved" if aprobar else "rejected",
        "comment": "Desde Ajustes del servidor"})


def aprobar_cuando_llegue(d: dict, desde: str, cada: int = 15, hasta: int = 900, dormir=time.sleep) -> bool:
    """Quien pulsa "Pasar a PRO" ya ha decidido: cuando la ejecucion pedida
    (la primera a mano desde 'desde') espera aprobacion para pro, se aprueba."""
    t = 0
    while t <= hasta:
        try:
            datos = llamar("GET", f"/repos/{d['repo']}/actions/workflows/{d['workflow']}/runs"
                                  "?event=workflow_dispatch&per_page=10") or {}
            for r in datos.get("workflow_runs", []):
                if (r.get("created_at") or "") < desde:
                    continue
                if r.get("status") == "completed":
                    return False
                if r.get("status") == "waiting":
                    for p in llamar("GET", f"/repos/{d['repo']}/actions/runs/{r['id']}/pending_deployments") or []:
                        if p["environment"]["name"] == "pro" and p.get("current_user_can_approve"):
                            revisar(d["repo"], r["id"], p["environment"]["id"], True)
                            log.info("aprobado para PRO: %s", d["repo"])
                            return True
        except GitHubError as e:
            log.warning("aprobar: %s", e)
        dormir(cada)
        t += cada
    return False


def pasar_a_pro(d: dict, version: str) -> None:
    desde = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lanzar(d, "pro", version)
    threading.Thread(target=aprobar_cuando_llegue, args=(d, desde), daemon=True).start()
