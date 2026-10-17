# -*- coding: utf-8 -*-
"""Portada (/) y Ajustes (/ajustes/) del servidor.

La entrada de cada entorno (nginx, ver servidor.py) manda aqui "/" (como
/portada/) y "/ajustes/", con X-Entorno (dev o pro); la portada de una app
principal (p. ej. /spsil/, o / de su dominio) llega ademas con X-Principal.
El login es el de Keycloak (cliente propio, Authorization Code; el dominio de
una principal tiene el suyo): nadie ve nada sin entrar, cada uno ve solo las
apps de sus grupos (/DEV/<Grupo>, /PRO/<Grupo>, /PRO/<Principal>/<Grupo> o
un subgrupo) e IT (GRUPO_IT) las ve todas y entra en Ajustes.

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

from . import en_curso, github, guardado

ENTORNOS = ("dev", "pro")
NOMBRES = {"dev": "DEV", "pro": "PRO"}
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUTA_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
ID_RE = re.compile(r"^(?:[a-z][a-z0-9-]{1,30}\.)?[a-z][a-z0-9-]{1,30}$")
FICHERO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


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


def principales(cat: dict | None = None) -> dict:
    """{ruta: app} de las apps principales dadas de alta."""
    return {a["ruta"]: a for a in (cat or catalogo()).get("apps", []) if a.get("principal")}


def principal_de(request: Request) -> dict | None:
    """La app principal de esta peticion: X-Principal (lo pone la entrada) o su dominio."""
    ps = principales()
    p = request.headers.get("x-principal", "")
    if RUTA_RE.match(p) and p in ps:
        return ps[p]
    host = request.headers.get("host", "").split(":")[0].lower()
    return next((a for a in ps.values() if host and a.get("dominio") == host), None)


def en_dominio_de(request: Request) -> dict | None:
    """La principal si se esta entrando por su dominio (su login es otro cliente)."""
    p = principal_de(request)
    host = request.headers.get("host", "").split(":")[0].lower()
    return p if p and p.get("dominio") and host == p["dominio"] else None


def base_de(request: Request) -> str:
    p = en_dominio_de(request)
    return f"https://{p['dominio']}" if p else base_url(entorno_de(request))


def cliente_de(request: Request) -> tuple:
    """(client_id, secret) del login de esta portada."""
    p = en_dominio_de(request)
    if p:
        k = p["ruta"].upper().replace("-", "_")
        if env(f"KEYCLOAK_CLIENTE_{k}"):
            return env(f"KEYCLOAK_CLIENTE_{k}"), env(f"KEYCLOAK_SECRETO_{k}")
    return env("KEYCLOAK_CLIENTE", "servidor"), env("KEYCLOAK_SECRETO")


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


def puede_ver(grupos_app, mios: list, it: bool) -> bool:
    """grupos_app: uno o varios (camino entero); vale con estar en uno o en un subgrupo."""
    if it:
        return True
    lista = [grupos_app] if isinstance(grupos_app, str) else list(grupos_app or [])
    for g in (x.strip("/") for x in lista if isinstance(x, str)):
        if g and any(x == g or x.startswith(g + "/") for x in mios):
            return True
    return False


def _siguiente_valido(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def _kc(camino: str) -> str:
    """Las llamadas de servidor a servidor pueden ir por dentro (KEYCLOAK_INTERNO)."""
    return (env("KEYCLOAK_INTERNO") or env("KEYCLOAK_ISSUER")).rstrip("/") + camino


@app.get("/ajustes/entrar")
def entrar(request: Request, next: str = "/"):
    estado = secrets.token_urlsafe(24)
    base = base_de(request)
    request.session["login"] = {"estado": estado, "next": _siguiente_valido(next), "base": base}
    q = urlencode({"client_id": cliente_de(request)[0], "response_type": "code", "scope": "openid",
                   "redirect_uri": f"{base}/ajustes/entrar/vuelta", "state": estado})
    return RedirectResponse(f"{env('KEYCLOAK_ISSUER').rstrip('/')}/protocol/openid-connect/auth?{q}", status_code=303)


def canjear_codigo(code: str, redirect_uri: str, cliente: tuple = ()) -> dict:
    """{usuario, grupos, id_token} o {} si Keycloak no lo acepta."""
    cid, secreto = cliente or (env("KEYCLOAK_CLIENTE", "servidor"), env("KEYCLOAK_SECRETO"))
    try:
        r = httpx.post(_kc("/protocol/openid-connect/token"), timeout=10, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": cid, "client_secret": secreto})
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
    if not code or not state or not secrets.compare_digest(state, login.get("estado", "")):
        return RedirectResponse("/ajustes/entrar?next=/", status_code=303)
    base = base_de(request)   # la de esta peticion, no la de la sesion (que la pone el navegador)
    datos = canjear_codigo(code, f"{base}/ajustes/entrar/vuelta", cliente_de(request))
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
    id_token = request.session.get("id_token", "")
    request.session.clear()
    p = principal_de(request)
    vuelta = f"{base_de(request)}/" if en_dominio_de(request) or not p else f"{base_de(request)}/{p['ruta']}/"
    q = {"client_id": cliente_de(request)[0], "post_logout_redirect_uri": vuelta}
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


def _enlace(a: dict, host_aqui: str) -> str:
    """A donde lleva su tarjeta: su camino, o su dominio si esta en otro."""
    cam = a.get("camino") or f"/{a['ruta']}/"
    h = a.get("host") or ""
    return cam if not h or h == host_aqui else f"https://{h}{cam}"


@app.get("/portada/")
def portada(request: Request):
    p = principal_de(request)
    inicio = "/" if not p or en_dominio_de(request) else f"/{p['ruta']}/"
    if not usuario(request):
        return RedirectResponse(f"/ajustes/entrar?next={inicio}", status_code=303, headers={"Cache-Control": "no-store"})
    ent = entorno_de(request)
    it = es_it(request)
    host = request.headers.get("host", "").split(":")[0].lower()
    todas = catalogo().get("entradas", {}).get(ent, [])
    mios = grupos(request)
    if p:
        apps = [a for a in todas if a.get("dentro") == p["ruta"] and puede_ver(a.get("grupos") or a.get("grupo", ""), mios, it)]
        titulo, sub = p["nombre"], ("" if en_dominio_de(request) else f"{SUBTITULOS[ent]}")
    else:
        dentro = {}
        for a in todas:
            if a.get("dentro") and puede_ver(a.get("grupos") or a.get("grupo", ""), mios, it):
                dentro[a["dentro"]] = True
        apps = [a for a in todas if not a.get("dentro") and (
            puede_ver(a.get("grupos") or a.get("grupo", ""), mios, it) or (a.get("principal") and dentro.get(a["ruta"])))]
        titulo, sub = TITULOS[ent], SUBTITULOS[ent]
    apps = [{**a, "enlace": _enlace(a, host),
             "salud_url": "" if a.get("principal") else _enlace(a, host) + (a.get("salud") or "version/")} for a in apps]
    logo = (p or {}).get("logo", "") if en_dominio_de(request) else ""
    r = plantillas.TemplateResponse(request, "portada.html", {
        "usuario": usuario(request), "it": it, "apps": apps, "entorno": ent, "titulo": titulo, "sub": sub,
        "logo": f"/{logo}" if logo and FICHERO_RE.match(logo) else "", "propio": bool(en_dominio_de(request)),
        "ajustes": f"{base_url(env('AJUSTES_EN', 'pro'))}/ajustes/" if base_url(env("AJUSTES_EN", "pro")) else "/ajustes/"})
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/ajustes/estaticos/{ruta}/{fichero:path}")
def estatico(ruta: str, fichero: str):
    """Logo y /marca/ de la portada de una principal en su dominio (los copia el servidor)."""
    partes = fichero.split("/")
    if not RUTA_RE.match(ruta) or not partes or not all(FICHERO_RE.match(x) for x in partes):
        return Response(status_code=404)
    base = (_dir("ESTADO_DIR", "/estado") / "estaticos" / ruta).resolve()
    f = (base / fichero).resolve()
    if not f.is_relative_to(base) or not f.is_file():
        return Response(status_code=404)
    tipos = {"webp": "image/webp", "png": "image/png", "svg": "image/svg+xml", "ico": "image/x-icon", "jpg": "image/jpeg",
             "jpeg": "image/jpeg", "css": "text/css", "js": "text/javascript", "woff": "font/woff", "woff2": "font/woff2"}
    return Response(f.read_bytes(), media_type=tipos.get(f.suffix.lstrip(".").lower(), "application/octet-stream"),
                    headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})


# ── Ajustes (solo IT) ────────────────────────────────────────────────────────

def version_en(ent: str, ruta: str, camino: str, host: str = "") -> dict:
    """Lo que dice la app en ese entorno, preguntando a su entrada por dentro.
    ruta: 'tareas' o 'spsil/tareas' (sin barras); host: su dominio si va por otro."""
    try:
        r = httpx.get(f"http://entrada-{ent}/{ruta.strip('/')}/{camino.lstrip('/')}", timeout=4, follow_redirects=False,
                      headers={"Host": host or dominio(ent) or "localhost", "X-Forwarded-Proto": "https", "Accept": "application/json"})
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
    lista, resultados = [], _resultados(30)
    for a in cat.get("apps", []):
        if not a.get("principal"):
            lista.append({**a, "id": a.get("id") or a["ruta"], "dentro": a.get("dentro") or "", "alta": True})
    for s in cat.get("sin_alta", []):
        nombre = next((x["nombre"] for e in ENTORNOS for x in cat.get("entradas", {}).get(e, []) if x["ruta"] == s["ruta"]), s["ruta"])
        salud = next((x["salud"] for e in ENTORNOS for x in cat.get("entradas", {}).get(e, []) if x["ruta"] == s["ruta"]), "version/")
        lista.append({"ruta": s["ruta"], "id": s["ruta"], "dentro": "", "nombre": nombre, "grupo": s.get("grupo", ""), "version": salud,
                      "despliegue": {"tipo": "ninguno"}, "alta": False, "repo": "", "dominio": ""})

    def donde(t: dict, e: str) -> dict:
        d = (t.get("donde") or {}).get(e) or {}
        return {"host": d.get("host") or dominio(e), "camino": d.get("camino") or f"/{t['ruta']}/"}
    trabajos = [(t, e) for t in lista for e in ENTORNOS]
    with ThreadPoolExecutor(max_workers=8) as pool:
        vers = list(pool.map(lambda te: version_en(te[1], donde(*te)["camino"], te[0].get("version") or "version/",
                                                   donde(*te)["host"]), trabajos))
    for t in lista:
        t["entornos"] = []
    for (t, e), v in zip(trabajos, vers):
        w = donde(t, e)
        t["entornos"].append({"nombre": e, "url": f"https://{w['host']}{w['camino']}" if w["host"] else "", **v})
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
        t["version_pro"] = next((e["version"] for e in t["entornos"] if e["nombre"] == "pro"), "")
        # lo que GitHub esta desplegando (no las pruebas de un PR)
        t["en_marcha_github"] = next((r for r in t["ejecuciones"] if r.get("evento") in ("push", "workflow_dispatch", "release")
                                      and r.get("estado") in ("queued", "in_progress", "waiting", "pending", "requested")), None)
        t["en_curso"] = None
        if t.get("alta"):
            ent = (en_curso.leer().get(t["id"]) or {}).get("entorno", "")
            viva = next((e["version"] for e in t["entornos"] if e["nombre"] == ent), "")
            t["en_curso"] = en_curso.resolver(t["id"], d["tipo"], viva, t["ejecuciones"], resultados)
        vistas = []
        for e in ENTORNOS:
            x = next((x for x in cat.get("entradas", {}).get(e, []) if (x.get("id") or x["ruta"]) == t["id"]), None)
            vistas += (x.get("grupos") or ([x["grupo"]] if x.get("grupo") else [])) if x else []
        t["vista_por"] = list(dict.fromkeys(vistas))
        t["ocupada"] = bool((t["en_curso"] and not t["en_curso"].get("fallo")) or t["en_marcha_github"])
    return lista


def secciones(cat: dict, lista: list) -> list:
    """Ajustes por apps: las sueltas y luego cada principal con sus subapps."""
    salida = [{"principal": None, "tarjetas": [t for t in lista if not t.get("dentro")]}]
    for p in principales(cat).values():
        salida.append({"principal": p, "tarjetas": [t for t in lista if t.get("dentro") == p["ruta"]]})
    return salida


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
    lista = tarjetas(cat)
    r = plantillas.TemplateResponse(request, "ajustes.html", {
        "usuario": usuario(request), "sin_permiso": False, "cat": cat, "tarjetas": lista,
        "secciones": secciones(cat, lista), "principales": sorted(principales(cat)),
        "token": guardado.hay_token(), "en_cola": en_cola, "resultados": _resultados(),
        "algo_en_curso": bool(en_curso.leer()),
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


def _reservar(request: Request, a: dict, ent: str, version: str) -> bool:
    """Que no se pidan dos despliegues de la misma app a la vez (en ninguna pestaña)."""
    otro = en_curso.reservar(a["id"], ent, version, usuario(request))
    if otro:
        avisar(request, "mal", f"{a['nombre']} ya se está desplegando ({otro['version']} en {NOMBRES.get(otro['entorno'], otro['entorno'])}, "
                               f"pedido por {otro['quien']}). Espera a que acabe.")
        return False
    return True


def _volver() -> RedirectResponse:
    return RedirectResponse("/ajustes/", status_code=303)


def _app(ida: str) -> dict | None:
    """La app dada de alta con ese id (su ruta; una subapp, principal.ruta)."""
    for a in catalogo().get("apps", []):
        a = {**a, "id": a.get("id") or a["ruta"], "dentro": a.get("dentro") or ""}
        if a["id"] == ida:
            return a
    return None


def _id(form) -> str:
    v = str(form.get("id") or form.get("ruta") or "")
    return v if ID_RE.match(v) else ""


def _donde(a: dict, ent: str) -> tuple:
    d = (a.get("donde") or {}).get(ent) or {}
    return d.get("camino") or f"/{a['ruta']}/", d.get("host") or ""


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
    dentro = str(form.get("dentro", "")).strip()
    if dentro and dentro not in principales():
        avisar(request, "mal", "Esa app principal no existe.")
        return _volver()
    try:
        apps = github.leer_manifiesto(repo, rama)
    except github.GitHubError as e:
        avisar(request, "mal", f"{repo}: {e}")
        return _volver()
    solo = str(form.get("solo", "")).strip().strip("/")
    if solo:
        apps = [m for m in apps if str(m.get("ruta", "")).strip("/") == solo]
        if not apps:
            avisar(request, "mal", f"{repo} no tiene ninguna app con ruta /{solo}/ en su servidor.json.")
            return _volver()
    for m in apps:
        datos = {"repo": repo, "manifiesto": m}
        if dentro:
            datos["dentro"] = dentro
        for k in ("contenedor_dev", "contenedor_pro"):   # si en un entorno ya se llama distinto
            if len(apps) == 1 and str(form.get(k, "")).strip():
                datos[k] = str(form.get(k)).strip()
        if str(form.get("grupos_extra", "")).strip():
            datos["grupos_extra"] = str(form.get("grupos_extra"))
        pedir(request, "registrar", datos)
    rutas = ", ".join(f"/{dentro + '/' if dentro else ''}{m.get('ruta')}/" for m in apps)
    avisar(request, "bien", f"Pedido: dar de alta {rutas} desde {repo}.")
    return _volver()


@app.post("/ajustes/apps/principal/")
async def nueva_principal(request: Request):
    """Una app principal (p. ej. SPSIL): su portada en /<ruta>/ y dentro sus subapps."""
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    ruta = str(form.get("ruta", "")).strip().strip("/").lower()
    if not RUTA_RE.match(ruta):
        avisar(request, "mal", "La ruta va en minúsculas, números y guiones (p. ej. spsil).")
        return _volver()
    pedir(request, "principal", {"ruta": ruta, "nombre": str(form.get("nombre", "")).strip(),
                                 "grupo": str(form.get("grupo", "")).strip()})
    avisar(request, "bien", f"Pedido: app principal /{ruta}/. Luego añádele sus subapps eligiéndola en «Dentro de».")
    return _volver()


@app.post("/ajustes/apps/principal/ajustes/")
async def ajustes_principal(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "principal_ajustes", {"id": _id(form), "tema": str(form.get("tema", "")), "logo": str(form.get("logo", "")),
                                         "redirigir": str(form.get("redirigir", ""))})
    avisar(request, "bien", "Pedido: guardar su portada (tema, logo y rutas viejas).")
    return _volver()


@app.post("/ajustes/apps/quitar/")
async def quitar(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "quitar", {"id": _id(form)})
    avisar(request, "bien", "Pedido: quitarla del panel y de la entrada (su contenedor y sus grupos no se tocan).")
    return _volver()


@app.post("/ajustes/apps/grupo/")
async def grupo(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    datos = {"id": _id(form), "grupo": str(form.get("grupo", ""))}
    if "grupos_extra" in form:
        datos["grupos_extra"] = str(form.get("grupos_extra", ""))
    pedir(request, "grupo", datos)
    avisar(request, "bien", "Pedido: cambiar quién la ve en la portada.")
    return _volver()


@app.post("/ajustes/apps/dominio/")
async def poner_dominio(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "dominio", {"id": _id(form), "dominio": str(form.get("dominio", ""))})
    avisar(request, "bien", "Pedido: PRO en su dominio (DNS, certificado y login). La tarjeta dirá cómo va.")
    return _volver()


@app.post("/ajustes/apps/sincronizar/")
async def sincronizar(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "sincronizar", {"id": _id(form)})
    avisar(request, "bien", "Pedido: revisar su dominio y arreglar lo que falte.")
    return _volver()


@app.post("/ajustes/apps/quitar-dominio/")
async def quitar_dominio(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    pedir(request, "quitar_dominio", {"id": _id(form)})
    avisar(request, "bien", "Pedido: quitarle su dominio (vuelve a donde iba antes).")
    return _volver()


@app.post("/ajustes/cloudflare/")
async def cloudflare(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    tok = str(form.get("token", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,200}", tok):
        avisar(request, "mal", "Eso no parece un token de Cloudflare.")
        return _volver()
    pedir(request, "cloudflare", {"token": tok})
    avisar(request, "bien", "Pedido: guardar el token de Cloudflare (solo lo lee el servidor).")
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
    a = _app(_id(form))
    ent, version = str(form.get("entorno", "")), str(form.get("version", "")).strip()
    if not a or ent not in ENTORNOS or not VERSION_RE.match(version):
        avisar(request, "mal", "Datos no válidos.")
        return _volver()
    d = a["despliegue"]
    if d["tipo"] == "workflow" and version == "anterior" and not d.get("anterior"):
        avisar(request, "mal", f"El workflow de {a['nombre']} no sabe volver a la anterior: escribe la versión.")
        return _volver()
    if d["tipo"] not in ("workflow", "servidor"):
        avisar(request, "mal", f"{a['nombre']} no dice cómo se despliega.")
        return _volver()
    if not _reservar(request, a, ent, version):
        return _volver()
    try:
        if d["tipo"] == "workflow":
            github.lanzar(d, ent, version)
        elif d["tipo"] == "servidor":
            pedir(request, "desplegar", {"id": a["id"], "entorno": ent, "version": version})
        else:
            raise github.GitHubError(f"{a['nombre']} no dice cómo se despliega.")
        que = "la versión anterior" if version == "anterior" else version
        avisar(request, "bien", f"Pedido: {que} de {a['nombre']} en {NOMBRES[ent]}. Tarda unos minutos.")
    except github.GitHubError as e:
        en_curso.soltar(a["id"])
        avisar(request, "mal", str(e))
    return _volver()


@app.post("/ajustes/pasar-a-pro/")
async def pasar_a_pro(request: Request):
    """El boton: lo que tiene DEV pasa a PRO (y si GitHub pide aprobacion, se aprueba sola)."""
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    a = _app(_id(form))
    if not a or a.get("principal"):
        avisar(request, "mal", "Esa app no está dada de alta.")
        return _volver()
    d = a["despliegue"]
    if d["tipo"] == "servidor":
        v = (a.get("versiones") or {}).get("dev", "")
    else:
        v = version_en("dev", _donde(a, "dev")[0], a.get("version") or "version/", _donde(a, "dev")[1])["version"]
    if not VERSION_RE.match(v or "") or v == "anterior":
        avisar(request, "mal", f"No sé qué versión tiene {a['nombre']} en DEV: escríbela en «Poner» de PRO.")
        return _volver()
    if d["tipo"] == "servidor":
        en_pro = (a.get("versiones") or {}).get("pro", "")
    else:
        en_pro = version_en("pro", _donde(a, "pro")[0], a.get("version") or "version/", _donde(a, "pro")[1])["version"]
    if en_pro == v:
        avisar(request, "bien", f"PRO ya tiene {v} de {a['nombre']}, lo mismo que DEV: no hay nada que pasar.")
        return _volver()
    if d["tipo"] not in ("workflow", "servidor"):
        avisar(request, "mal", f"{a['nombre']} no dice cómo se despliega.")
        return _volver()
    if not _reservar(request, a, "pro", v):
        return _volver()
    try:
        if d["tipo"] == "workflow":
            github.pasar_a_pro(d, v)
        elif d["tipo"] == "servidor":
            pedir(request, "desplegar", {"id": a["id"], "entorno": "pro", "version": v})
        else:
            raise github.GitHubError(f"{a['nombre']} no dice cómo se despliega.")
        avisar(request, "bien", f"Pedido: {v} de {a['nombre']} a PRO. Tarda unos minutos; la tarjeta lo dirá.")
    except github.GitHubError as e:
        en_curso.soltar(a["id"])
        avisar(request, "mal", str(e))
    return _volver()


@app.post("/ajustes/revisar/")
async def revisar(request: Request):
    form = await _form(request)
    if form is None:
        return Response(status_code=403)
    a = _app(_id(form))
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
    return JSONResponse({"en_cola": _peticiones_en_cola(), "ultimo": (_resultados(1) or [{}])[0].get("id", ""),
                         "en_curso": sorted(en_curso.leer())},
                        headers={"Cache-Control": "no-store"})

