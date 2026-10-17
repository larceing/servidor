# -*- coding: utf-8 -*-
"""Portada (/) y Ajustes (/ajustes/) del servidor.

La entrada de cada entorno (nginx, ver servidor.py) manda aqui "/" (como
/portada/) y "/ajustes/", con X-Entorno (dev o pro). El login es el de
Keycloak (cliente propio, Authorization Code): nadie ve nada sin entrar,
cada uno ve solo las apps de sus grupos (/DEV/<Grupo>, /PRO/<Grupo> o un
subgrupo) e IT (GRUPO_IT) las ve todas y entra en Ajustes.

Lo que sabe del servidor lo lee de /estado/catalogo.json (lo escribe el
agente); lo que pide al servidor (dar de alta una app, un dominio, un
grupo...) lo deja en /peticiones/ y el agente lo hace. Los despliegues de
las apps con su propio workflow van directos a GitHub."""
from __future__ import annotations

import json
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import github, guardado

ENTORNOS = ("dev", "pro")
NOMBRES = {"dev": "DEV", "pro": "PRO"}
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUTA_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


def env(k: str, defecto: str = "") -> str:
    return os.environ.get(k, defecto).strip()


def _dir(k: str, defecto: str) -> Path:
    return Path(os.environ.get(k, defecto))


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=env("PANEL_CLAVE") or secrets.token_hex(32),
                   session_cookie="servidor_sesion", max_age=12 * 3600, same_site="lax",
                   https_only=env("SESION_SEGURA", "1") == "1")
plantillas = Jinja2Templates(directory=str(Path(__file__).parent / "plantillas"))


# ── datos del servidor ───────────────────────────────────────────────────────

def catalogo() -> dict:
    try:
        return json.loads((_dir("ESTADO_DIR", "/estado") / "catalogo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entradas": {}, "apps": [], "sin_alta": [], "dominios": {}}


def dominio(ent: str) -> str:
    return env(f"DOMINIO_{ent.upper()}")


def entorno_de(request: Request) -> str:
    e = request.headers.get("x-entorno", "")
    if e in ENTORNOS:
        return e
    host = request.headers.get("host", "").split(":")[0].lower()
    for ent in ENTORNOS:
        if host and host == dominio(ent):
            return ent
    return env("AJUSTES_EN", "pro") if env("AJUSTES_EN") in ENTORNOS else "pro"


def base_url(ent: str) -> str:
    d = dominio(ent)
    return f"https://{d}" if d else ""


# ── sesion y login con Keycloak ──────────────────────────────────────────────

def usuario(request: Request) -> str:
    return request.session.get("usuario", "")


def grupos(request: Request) -> list:
    return request.session.get("grupos") or []


def es_it(request: Request) -> bool:
    it = env("GRUPO_IT", "IT").strip("/")
    return bool(it) and it in grupos(request)


def normalizar(gs) -> list:
    return sorted({g.strip().strip("/") for g in (gs or []) if isinstance(g, str) and g.strip().strip("/")})


def puede_ver(grupo_app: str, mios: list, it: bool) -> bool:
    if it:
        return True
    g = (grupo_app or "").strip("/")
    return bool(g) and any(x == g or x.startswith(g + "/") for x in mios)


def _siguiente_valido(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def _kc(camino: str) -> str:
    """Las llamadas de servidor a servidor pueden ir por dentro (KEYCLOAK_INTERNO)."""
    return (env("KEYCLOAK_INTERNO") or env("KEYCLOAK_ISSUER")).rstrip("/") + camino


@app.get("/ajustes/entrar")
def entrar(request: Request, next: str = "/"):
    ent = entorno_de(request)
    estado = secrets.token_urlsafe(24)
    request.session["login"] = {"estado": estado, "next": _siguiente_valido(next), "ent": ent}
    q = urlencode({"client_id": env("KEYCLOAK_CLIENTE", "servidor"), "response_type": "code", "scope": "openid",
                   "redirect_uri": f"{base_url(ent)}/ajustes/entrar/vuelta", "state": estado})
    return RedirectResponse(f"{env('KEYCLOAK_ISSUER').rstrip('/')}/protocol/openid-connect/auth?{q}", status_code=303)


def canjear_codigo(code: str, redirect_uri: str) -> dict:
    """{usuario, grupos, id_token} o {} si Keycloak no lo acepta."""
    try:
        r = httpx.post(_kc("/protocol/openid-connect/token"), timeout=10, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": env("KEYCLOAK_CLIENTE", "servidor"), "client_secret": env("KEYCLOAK_SECRETO")})
        if r.status_code != 200:
            return {}
        tok = r.json()
        u = httpx.get(_kc("/protocol/openid-connect/userinfo"), timeout=10,
                      headers={"Authorization": f"Bearer {tok['access_token']}"})
        if u.status_code != 200:
            return {}
        datos = u.json()
    except (httpx.HTTPError, ValueError, KeyError):
        return {}
    if not datos.get("preferred_username"):
        return {}
    return {"usuario": datos["preferred_username"], "grupos": normalizar(datos.get("groups")),
            "id_token": tok.get("id_token") or ""}


@app.get("/ajustes/entrar/vuelta")
def vuelta(request: Request, code: str = "", state: str = ""):
    login = request.session.pop("login", None) or {}
    ent = login.get("ent") or entorno_de(request)
    if not code or not state or not secrets.compare_digest(state, login.get("estado", "")):
        return RedirectResponse("/ajustes/entrar?next=/", status_code=303)
    datos = canjear_codigo(code, f"{base_url(ent)}/ajustes/entrar/vuelta")
    if not datos:
        return Response("Keycloak no ha aceptado el inicio de sesión. Vuelve a intentarlo desde el inicio.",
                        status_code=400, media_type="text/plain; charset=utf-8")
    request.session.clear()
    request.session.update({"usuario": datos["usuario"], "grupos": datos["grupos"],
                            "csrf": secrets.token_urlsafe(24),
                            # sin el id_token Keycloak pregunta al salir; si es enorme no cabe en la cookie
                            "id_token": datos["id_token"] if len(datos["id_token"]) < 1800 else ""})
    return RedirectResponse(login.get("next") or "/", status_code=303)


@app.get("/ajustes/salir")
def salir(request: Request):
    ent = entorno_de(request)
    id_token = request.session.get("id_token", "")
    request.session.clear()
    q = {"client_id": env("KEYCLOAK_CLIENTE", "servidor"), "post_logout_redirect_uri": f"{base_url(ent)}/"}
    if id_token:
        q["id_token_hint"] = id_token
    return RedirectResponse(f"{env('KEYCLOAK_ISSUER').rstrip('/')}/protocol/openid-connect/logout?{urlencode(q)}",
                            status_code=303)


@app.api_route("/ajustes/salud", methods=["GET", "HEAD"])
def salud():
    return JSONResponse({"ok": True, "version": env("VERSION", "local")})


# ── portada ──────────────────────────────────────────────────────────────────

TITULOS = {"pro": "Producción", "dev": "Pruebas"}
SUBTITULOS = {"pro": "Las aplicaciones en uso.", "dev": "Versiones en pruebas: los datos de aquí no son los reales."}


@app.get("/portada/")
def portada(request: Request):
    if not usuario(request):
        return RedirectResponse("/ajustes/entrar?next=/", status_code=303, headers={"Cache-Control": "no-store"})
    ent = entorno_de(request)
    it = es_it(request)
    apps = [a for a in catalogo().get("entradas", {}).get(ent, []) if puede_ver(a.get("grupo", ""), grupos(request), it)]
    r = plantillas.TemplateResponse(request, "portada.html", {
        "usuario": usuario(request), "it": it, "apps": apps, "entorno": ent,
        "titulo": TITULOS[ent], "sub": SUBTITULOS[ent],
        "ajustes": f"{base_url(env('AJUSTES_EN', 'pro'))}/ajustes/" if base_url(env("AJUSTES_EN", "pro")) else "/ajustes/"})
    r.headers["Cache-Control"] = "no-store"
    return r


# ── Ajustes (solo IT) ────────────────────────────────────────────────────────

def version_en(ent: str, ruta: str, camino: str) -> dict:
    """Lo que dice la app en ese entorno, preguntando a su entrada por dentro."""
    try:
        r = httpx.get(f"http://entrada-{ent}/{ruta}/{camino.lstrip('/')}", timeout=4, follow_redirects=False,
                      headers={"Host": dominio(ent) or "localhost", "X-Forwarded-Proto": "https", "Accept": "application/json"})
    except httpx.HTTPError:
        return {"ok": False, "version": "", "detalle": "no responde"}
    if r.status_code != 200:
        return {"ok": False, "version": "", "detalle": f"responde {r.status_code}"}
    try:
        d = r.json()
        v = str(d.get("version") or "") if isinstance(d, dict) else ""
    except ValueError:
        v = ""
    return {"ok": True, "version": v if VERSION_RE.match(v) else "", "detalle": "" if v else "responde, sin decir versión"}


def _peticiones_en_cola() -> list:
    d = _dir("PETICIONES_DIR", "/peticiones")
    try:
        return sorted(p.stem for p in d.glob("*.json"))
    except OSError:
        return []


def _resultados(limite: int = 10) -> list:
    d = _dir("ESTADO_DIR", "/estado") / "resultados"
    salida = []
    try:
        for p in sorted(d.glob("*.json"), reverse=True)[:limite]:
            try:
                salida.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                pass
    except OSError:
        pass
    return salida


def pedir(request: Request, accion: str, datos: dict) -> str:
    """Deja la peticion para el agente del servidor (escritura atomica)."""
    d = _dir("PETICIONES_DIR", "/peticiones")
    pid = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f") + "-" + secrets.token_hex(3)
    tmp = d / f".{pid}.tmp"
    tmp.write_text(json.dumps({"accion": accion, "datos": datos, "quien": usuario(request),
                               "fecha": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    os.replace(tmp, d / f"{pid}.json")
    return pid


def _agente_vivo(cat: dict) -> bool:
    latido = cat.get("latido", "")
    hace = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bool(latido) and latido >= hace


def tarjetas(cat: dict) -> list:
    """Una por app: las dadas de alta y las que solo tienen su ruta de antes."""
    lista = []
    for a in cat.get("apps", []):
        lista.append({**a, "alta": True})
    for s in cat.get("sin_alta", []):
        nombre = next((x["nombre"] for e in ENTORNOS for x in cat.get("entradas", {}).get(e, []) if x["ruta"] == s["ruta"]), s["ruta"])
        salud = next((x["salud"] for e in ENTORNOS for x in cat.get("entradas", {}).get(e, []) if x["ruta"] == s["ruta"]), "version/")
        lista.append({"ruta": s["ruta"], "nombre": nombre, "grupo": s.get("grupo", ""), "version": salud,
                      "despliegue": {"tipo": "ninguno"}, "alta": False, "repo": "", "dominio": ""})
    trabajos = [(t, e) for t in lista for e in ENTORNOS]
    with ThreadPoolExecutor(max_workers=8) as pool:
        vers = list(pool.map(lambda te: version_en(te[1], te[0]["ruta"], te[0].get("version") or "version/"), trabajos))
    for t in lista:
        t["entornos"] = []
    for (t, e), v in zip(trabajos, vers):
        t["entornos"].append({"nombre": e, "url": f"{base_url(e)}/{t['ruta']}/" if base_url(e) else "", **v})
    for t in lista:
        d = t["despliegue"]
        t["pendientes"], t["ejecuciones"], t["error"] = [], [], ""
        if d["tipo"] == "workflow" and guardado.hay_token():
            try:
                t["pendientes"] = github.pendientes(d)
                t["ejecuciones"] = github.ejecuciones(d)
            except github.GitHubError as e:
                t["error"] = str(e)
        if d["tipo"] == "servidor":
            vs = t.get("versiones", {})
            for e in t["entornos"]:
                e["version"] = e["version"] or vs.get(e["nombre"], "")
        t["version_dev"] = next((e["version"] for e in t["entornos"] if e["nombre"] == "dev"), "")
    return lista


def _solo_it(request: Request):
    """None si vale; si no, la respuesta que toca."""
    ent = entorno_de(request)
    if ent != env("AJUSTES_EN", "pro") and base_url(env("AJUSTES_EN", "pro")):
        return RedirectResponse(f"{base_url(env('AJUSTES_EN', 'pro'))}/ajustes/", status_code=303)
    if not usuario(request):
        return RedirectResponse("/ajustes/entrar?next=/ajustes/", status_code=303)
    if not es_it(request):
        return plantillas.TemplateResponse(request, "ajustes.html", {"usuario": usuario(request), "sin_permiso": True},
                                           status_code=403)
    return None


def avisar(request: Request, tipo: str, texto: str) -> None:
    request.session.setdefault("avisos", []).append({"tipo": tipo, "texto": texto[:600]})


@app.get("/ajustes/")
def ajustes(request: Request):
    no = _solo_it(request)
    if no is not None:
        return no
    cat = catalogo()
    en_cola = _peticiones_en_cola()
    r = plantillas.TemplateResponse(request, "ajustes.html", {
        "usuario": usuario(request), "sin_permiso": False, "cat": cat, "tarjetas": tarjetas(cat),
        "token": guardado.hay_token(), "en_cola": en_cola, "resultados": _resultados(),
        "agente_vivo": _agente_vivo(cat), "avisos": request.session.pop("avisos", []),
        "csrf": request.session.setdefault("csrf", secrets.token_urlsafe(24)), "nombres": NOMBRES,
        "portada": "/", "version": env("VERSION", "local")})
    r.headers["Cache-Control"] = "no-store"
    return r


async def _form(request: Request):
    """El formulario, si es de IT y trae el csrf de su sesion; si no, None."""
    if _solo_it(request) is not None:
        return None
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "-")):
        return None
    return form


def _volver() -> RedirectResponse:
    return RedirectResponse("/ajustes/", status_code=303)


def _app(ruta: str) -> dict | None:
    return next((a for a in catalogo().get("apps", []) if a["ruta"] == ruta), None)


@app.post("/ajustes/github/")
async def guardar_github(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    tok = str(form.get("token", "")).strip()
    if tok:
        if not re.fullmatch(r"[A-Za-z0-9_]{20,255}", tok):
            avisar(request, "mal", "Eso no parece un token de GitHub.")
        else:
            guardado.guardar_token(tok)
            avisar(request, "bien", "Token de GitHub guardado.")
    return _volver()


@app.post("/ajustes/apps/anadir/")
async def anadir(request: Request):
    """Lee servidor.json del repositorio y pide al servidor que dé de alta sus apps."""
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    repo = re.sub(r"^https?://github\.com/", "", str(form.get("repo", "")).strip().strip("/")).removesuffix(".git")
    rama = str(form.get("rama", "")).strip()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or (rama and not re.fullmatch(r"[\w./-]{1,100}", rama)):
        avisar(request, "mal", "El repositorio va como usuario/nombre.")
        return _volver()
    try:
        apps = github.leer_manifiesto(repo, rama)
    except github.GitHubError as e:
        avisar(request, "mal", f"{repo}: {e}")
        return _volver()
    for m in apps:
        pedir(request, "registrar", {"repo": repo, "manifiesto": m})
    avisar(request, "bien", f"Pedido: dar de alta {', '.join('/' + str(m.get('ruta')) + '/' for m in apps)} desde {repo}.")
    return _volver()


@app.post("/ajustes/apps/quitar/")
async def quitar(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "quitar", {"ruta": str(form.get("ruta", ""))})
    avisar(request, "bien", "Pedido: quitarla del panel y de la entrada (su contenedor y sus grupos no se tocan).")
    return _volver()


@app.post("/ajustes/apps/grupo/")
async def grupo(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "grupo", {"ruta": str(form.get("ruta", "")), "grupo": str(form.get("grupo", ""))})
    avisar(request, "bien", "Pedido: cambiar quién la ve en la portada.")
    return _volver()


@app.post("/ajustes/apps/dominio/")
async def poner_dominio(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "dominio", {"ruta": str(form.get("ruta", "")), "dominio": str(form.get("dominio", ""))})
    avisar(request, "bien", "Pedido: dominio propio para PRO, con certificado y login.")
    return _volver()


@app.post("/ajustes/npm/")
async def npm(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "npm", {"email": str(form.get("email", "")), "clave": str(form.get("clave", ""))})
    avisar(request, "bien", "Pedido: guardar el acceso a Nginx Proxy Manager.")
    return _volver()


@app.post("/ajustes/poner/")
async def poner(request: Request):
    """Una version (o "anterior") de una app en un entorno."""
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    a = _app(str(form.get("ruta", "")))
    ent, version = str(form.get("entorno", "")), str(form.get("version", "")).strip()
    if not a or ent not in ENTORNOS or not VERSION_RE.match(version):
        avisar(request, "mal", "Datos no válidos.")
        return _volver()
    d = a["despliegue"]
    try:
        if d["tipo"] == "workflow":
            if version == "anterior" and not d.get("anterior"):
                raise github.GitHubError(f"El workflow de {a['nombre']} no sabe volver a la anterior: escribe la versión.")
            github.lanzar(d, ent, version)
        elif d["tipo"] == "servidor":
            pedir(request, "desplegar", {"ruta": a["ruta"], "entorno": ent, "version": version})
        else:
            raise github.GitHubError(f"{a['nombre']} no dice cómo se despliega.")
        que = "la versión anterior" if version == "anterior" else version
        avisar(request, "bien", f"Pedido: {que} de {a['nombre']} en {NOMBRES[ent]}. Tarda unos minutos.")
    except github.GitHubError as e:
        avisar(request, "mal", str(e))
    return _volver()


@app.post("/ajustes/pasar-a-pro/")
async def pasar_a_pro(request: Request):
    """El boton: lo que tiene DEV pasa a PRO (y si GitHub pide aprobacion, se aprueba sola)."""
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    a = _app(str(form.get("ruta", "")))
    if not a:
        avisar(request, "mal", "Esa app no está dada de alta.")
        return _volver()
    d = a["despliegue"]
    if d["tipo"] == "servidor":
        v = (a.get("versiones") or {}).get("dev", "")
    else:
        v = version_en("dev", a["ruta"], a.get("version") or "version/")["version"]
    if not VERSION_RE.match(v or "") or v == "anterior":
        avisar(request, "mal", f"No sé qué versión tiene {a['nombre']} en DEV: escríbela en «Poner» de PRO.")
        return _volver()
    try:
        if d["tipo"] == "workflow":
            github.pasar_a_pro(d, v)
        elif d["tipo"] == "servidor":
            pedir(request, "desplegar", {"ruta": a["ruta"], "entorno": "pro", "version": v})
        else:
            raise github.GitHubError(f"{a['nombre']} no dice cómo se despliega.")
        avisar(request, "bien", f"Pedido: {v} de {a['nombre']} a PRO. Tarda unos minutos; la tarjeta lo dirá.")
    except github.GitHubError as e:
        avisar(request, "mal", str(e))
    return _volver()


@app.post("/ajustes/revisar/")
async def revisar(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    a = _app(str(form.get("ruta", "")))
    aprobar = form.get("decision") == "aprobar"
    try:
        if not a or a["despliegue"]["tipo"] != "workflow":
            raise github.GitHubError("Esa app no se despliega con un workflow.")
        github.revisar(a["despliegue"]["repo"], int(form.get("run_id", 0)), int(form.get("entorno_id", 0)), aprobar)
        avisar(request, "bien", "Aprobado: se está desplegando." if aprobar else "Rechazado: no se despliega.")
    except (github.GitHubError, ValueError) as e:
        avisar(request, "mal", str(e) or "Datos no válidos.")
    return _volver()


@app.get("/ajustes/estado.json")
def estado(request: Request):
    """Para que la pagina sepa cuando el servidor ha terminado lo pedido."""
    if _solo_it(request) is not None:
        return JSONResponse({}, status_code=403)
    return JSONResponse({"en_cola": _peticiones_en_cola(), "ultimo": (_resultados(1) or [{}])[0].get("id", "")},
                        headers={"Cache-Control": "no-store"})

