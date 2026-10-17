#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""servidor: la parte comun del servidor DEV/PRO.

Hace lo que no es de ninguna app: la entrada de cada entorno (un nginx por
entorno que reparte por ruta: /tareas/, /linai/...), la portada (/) y
Ajustes (/ajustes/, el panel, que es otra app: carpeta panel/), los grupos y
clientes de Keycloak de cada app, los dominios propios y las apps que el
servidor despliega a partir de una imagen. No sabe nada del codigo de
ninguna app: cada una se da de alta con su manifiesto (servidor.json en su
repositorio, ver README.md).

Corre en el servidor como root:
  servidor estado                 que hay y como esta
  servidor instalar [dev|pro]     la primera vez (o para tomar otro entorno)
  servidor tomar ENT              esta entrada pasa a ser del servidor
  servidor soltar ENT             vuelve a la entrada de antes (marcha atras)
  servidor entrada                rehace la configuracion de las entradas
  servidor agente                 el servicio (systemd): peticiones del panel,
                                  sincroniza la entrada y se actualiza solo
  servidor actualizar             busca ya una version nueva de este repo
  servidor comprobar              prueba que este codigo funciona (sin tocar nada)

Todo vive en /opt/apps/servidor (SERVIDOR_DIR):
  servidor.env        configuracion (dominios, Keycloak, red...), la escribe instalar
  codigo/             este repositorio (lo actualiza el agente)
  registro/           un manifiesto por app dada de alta
  entrada/<ENT>/      la configuracion de nginx de cada entorno
  estado/             lo que lee el panel: catalogo.json y resultados/
  peticiones/         lo que pide el panel (crear, dominio, grupos...)
  apps/<ruta>/<ENT>/  las apps que despliega el servidor (tipo "servidor")
  secretos/           client secrets de Keycloak creados aqui (solo root)
"""
from __future__ import annotations

import base64
import datetime as _dt
import fcntl
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_POR_DEFECTO = "larceing/servidor"
DIR = Path(os.environ.get("SERVIDOR_DIR", "/opt/apps/servidor"))
ENTORNOS = ("dev", "pro")
IMAGEN_NGINX = "nginx:1.27-alpine"
PANEL = "servidor-panel"          # contenedor y nombre en la red del proxy
PANEL_UID = 10001                  # usuario del contenedor del panel
CADA_ACTUALIZAR = 60               # segundos entre busquedas de version nueva
CADA_DOMINIOS = 600
CARPETAS_FIJAS = ("marca", "js")   # carpetas del portal de antes que sirve la portada en su dominio                # cada cuanto se mira si los dominios propios siguen bien
ESPERA_SALUD = 300

RUTA = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
RESERVADAS = {"ajustes", "auth", "portada", "version", "no-disponible", "static", "salud"}
NOMBRE = re.compile(r"^[\w .()-]{1,60}$")
GRUPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,40}$")
CONTENEDOR = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
CAMINO = re.compile(r"^[A-Za-z0-9._/-]{0,100}$")
REPO = re.compile(r"^[\w.-]{1,100}/[\w.-]{1,100}$")
WORKFLOW = re.compile(r"^[\w.-]{1,100}\.ya?ml$")
RAMA = re.compile(r"^[\w./-]{1,100}$")
CAMPO = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,40}$")
IMAGEN = re.compile(r"^[a-z0-9][a-z0-9./_-]{2,200}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DOMINIO = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
ID_PETICION = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
ID_APP = re.compile(r"^(?:[a-z][a-z0-9-]{1,30}\.)?[a-z][a-z0-9-]{1,30}$")
ESTATICO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(webp|png|svg|ico|jpg|jpeg|css|js|woff2?)$")
DESTINO = re.compile(r"^(/[A-Za-z0-9._/-]{0,100}|https://[a-z0-9.-]+(/[A-Za-z0-9._/-]{0,100})?)$")


class Error(Exception):
    """Algo que se puede contar tal cual (en la terminal o en el panel)."""


_SALIDA: list | None = None   # lo que se va diciendo mientras se atiende una peticion


def log(msg: str) -> None:
    print(f"· {msg}", flush=True)
    if _SALIDA is not None:
        _SALIDA.append(msg)


def ahora() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list, entrada: str | None = None, ok=(0,), timeout: int = 600) -> str:
    """Ejecuta sin shell y devuelve la salida. Error si el codigo no es de 'ok'."""
    r = subprocess.run(cmd, input=entrada, capture_output=True, text=True, timeout=timeout)
    if r.returncode not in ok:
        detalle = (r.stderr or r.stdout).strip().splitlines()[-8:]
        raise Error(f"falló: {' '.join(str(c) for c in cmd[:4])}...\n" + "\n".join(detalle))
    return r.stdout


def leer_env(p: Path) -> dict:
    datos = {}
    try:
        for linea in p.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                datos[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return datos


def poner_env(p: Path, valores: dict) -> None:
    """Cambia o anade claves en un .env, dejando el resto como estaba."""
    lineas = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    pendientes = dict(valores)
    for i, linea in enumerate(lineas):
        k = linea.split("=", 1)[0].strip()
        if "=" in linea and not linea.lstrip().startswith("#") and k in pendientes:
            lineas[i] = f"{k}={pendientes.pop(k)}"
    lineas += [f"{k}={v}" for k, v in pendientes.items()]
    escribir(p, "\n".join(lineas) + "\n", modo=0o600)


def escribir(p: Path, texto: str, modo: int = 0o644) -> bool:
    """Escribe solo si cambia (atomico). True si ha cambiado."""
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.read_text(encoding="utf-8") == texto:
        return False
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.chmod(tmp, modo)
    os.replace(tmp, p)
    return True


# ── configuracion ────────────────────────────────────────────────────────────

def cfg() -> dict:
    c = leer_env(DIR / "servidor.env")
    c.setdefault("RED_PROXY", "proxy_default")
    c.setdefault("GRUPO_IT", "IT")
    c.setdefault("REPO", REPO_POR_DEFECTO)
    c.setdefault("RAMA", "main")
    c.setdefault("KEYCLOAK_CLIENTE", "servidor")
    c.setdefault("KEYCLOAK_CONTENEDOR", "keycloak")
    gest = [e for e in c.get("ENTORNOS_GESTIONADOS", "").replace(",", " ").split() if e in ENTORNOS]
    c["_gestionados"] = gest
    if c.get("AJUSTES_EN") not in ENTORNOS:
        c["AJUSTES_EN"] = "pro" if "pro" in gest else (gest[0] if gest else "pro")
    return c


def dominio(c: dict, ent: str) -> str:
    return c.get(f"DOMINIO_{ent.upper()}", "")


def legado(c: dict) -> Path | None:
    p = c.get("LEGADO", "")
    return Path(p) if p and Path(p).is_dir() else None


# ── manifiesto de una app ────────────────────────────────────────────────────
# Lo declara cada app en servidor.json (en la raiz de su repositorio). Ver
# README.md. Aqui se comprueba campo a campo: lo que no cuadra no entra.

def validar_manifiesto(m: dict, repo: str = "") -> dict:
    if not isinstance(m, dict):
        raise Error("el manifiesto tiene que ser un objeto JSON")
    ruta = str(m.get("ruta", "")).strip().strip("/").lower()
    if not RUTA.match(ruta) or ruta in RESERVADAS:
        raise Error(f"ruta no válida: '{ruta}' (minúsculas, números y guiones; no puede ser {', '.join(sorted(RESERVADAS))})")
    nombre = str(m.get("nombre") or ruta.capitalize()).strip()
    if not NOMBRE.match(nombre):
        raise Error(f"nombre no válido: '{nombre}'")
    grupo = str(m.get("grupo", "")).strip().strip("/")
    for pre in ("DEV/", "PRO/"):
        grupo = grupo.removeprefix(pre)
    if grupo and not GRUPO.match(grupo):
        raise Error(f"grupo no válido: '{grupo}' (p. ej. Tareas: lo ven /DEV/Tareas y /PRO/Tareas)")
    # una app principal (p. ej. SPSIL) agrupa subapps: su portada en /<ruta>/
    # y, en PRO, quiza su dominio; cada subapp va "dentro" de una
    principal = bool(m.get("principal", False))
    dentro = str(m.get("dentro") or "").strip().strip("/").lower()
    if dentro and (principal or not RUTA.match(dentro) or dentro in RESERVADAS or dentro == ruta):
        raise Error(f"dentro no válido: '{dentro}' (la ruta de su app principal)")
    contenedor = str(m.get("contenedor") or (f"{dentro}-{{ruta}}-{{entorno}}" if dentro else "{ruta}-{entorno}"))
    for ent in ENTORNOS:
        if not CONTENEDOR.match(contenedor.replace("{entorno}", ent).replace("{ruta}", ruta)):
            raise Error(f"contenedor no válido: '{contenedor}' (p. ej. {ruta}-{{entorno}})")
    try:
        puerto = int(m.get("puerto", 8000))
    except (TypeError, ValueError):
        raise Error("puerto no válido") from None
    if not 1 <= puerto <= 65535:
        raise Error("puerto no válido")
    prefijo = m.get("prefijo", "quitar")
    if prefijo not in ("quitar", "conservar"):
        raise Error("prefijo: 'quitar' (la app recibe /...) o 'conservar' (recibe /<ruta>/...)")
    entrada = m.get("entrada", "servidor")
    if entrada not in ("servidor", "propia"):
        raise Error("entrada: 'servidor' (la ruta la pone el servidor) o 'propia'")
    version = str(m.get("version", "version/")).strip().lstrip("/")
    if not CAMINO.match(version):
        raise Error(f"camino de la versión no válido: '{version}'")
    d = m.get("despliegue") or {}
    if not isinstance(d, dict):
        raise Error("despliegue tiene que ser un objeto")
    tipo = d.get("tipo", "workflow" if (d.get("workflow") or repo) else "ninguno")
    desp: dict = {"tipo": tipo}
    if tipo == "workflow":
        desp["repo"] = str(d.get("repo") or repo).strip()
        desp["workflow"] = str(d.get("workflow", "")).strip()
        desp["rama"] = str(d.get("rama") or "main").strip()
        desp["campo_entorno"] = str(d.get("campo_entorno") or "entorno").strip()
        desp["campo_version"] = str(d.get("campo_version") or "version").strip()
        desp["anterior"] = bool(d.get("anterior", False))
        if not REPO.match(desp["repo"]):
            raise Error(f"repositorio no válido: '{desp['repo']}' (usuario/nombre)")
        if not WORKFLOW.match(desp["workflow"]):
            raise Error(f"workflow no válido: '{desp['workflow']}' (el fichero de .github/workflows)")
        if not RAMA.match(desp["rama"]):
            raise Error("rama no válida")
        if not (CAMPO.match(desp["campo_entorno"]) and CAMPO.match(desp["campo_version"])) \
                or desp["campo_entorno"] == desp["campo_version"]:
            raise Error("los campos del workflow: letras, números, _ o -, y distintos")
    elif tipo == "servidor":
        desp["imagen"] = str(d.get("imagen", "")).strip().lower()
        if not IMAGEN.match(desp["imagen"]) or ":" in desp["imagen"]:
            raise Error(f"imagen no válida: '{desp['imagen']}' (sin versión, p. ej. ghcr.io/usuario/app)")
        try:
            desp["memoria"] = int(str(d.get("memoria_mb", 512)))
        except ValueError:
            raise Error("memoria_mb no válida") from None
        if not 64 <= desp["memoria"] <= 4096:
            raise Error("memoria_mb entre 64 y 4096")
        if entrada != "servidor":
            raise Error("una app que despliega el servidor tiene entrada 'servidor'")
    elif tipo != "ninguno":
        raise Error(f"despliegue.tipo no válido: '{tipo}' (workflow, servidor o ninguno)")
    kc = m.get("keycloak") or {}
    cliente = str(kc.get("cliente", "") if isinstance(kc, dict) else "").strip()
    if cliente and not CONTENEDOR.match(cliente.replace("{entorno}", "dev").replace("{ruta}", ruta)):
        raise Error(f"cliente de Keycloak no válido: '{cliente}' (p. ej. {ruta}-{{entorno}})")
    if tipo == "servidor" and not cliente:
        cliente = f"{dentro}-{{ruta}}-{{entorno}}" if dentro else "{ruta}-{entorno}"
    if principal and tipo != "ninguno":
        raise Error("una app principal no se despliega: se despliegan sus subapps")
    if dentro:
        entrada = "servidor"   # la ruta de una subapp siempre la pone el servidor
    m2 = {"ruta": ruta, "nombre": nombre, "grupo": grupo, "contenedor": contenedor, "puerto": puerto,
          "prefijo": prefijo, "entrada": entrada, "version": version, "despliegue": desp,
          "keycloak": {"cliente": cliente} if cliente else {}}
    if principal:
        m2["principal"] = True
    if dentro:
        m2["dentro"] = dentro
    return m2


def nombre_de(plantilla: str, ruta: str, ent: str) -> str:
    return plantilla.replace("{ruta}", ruta).replace("{entorno}", ent)


# ── registro: las apps dadas de alta ─────────────────────────────────────────

# Lo que se guarda de una app ademas de su manifiesto (y se conserva al releerlo):
#   dominio        PRO en su dominio (una app suelta o una principal con sus subapps)
#   contenedores   {entorno: contenedor} si en un entorno se llama distinto (lo que ya habia)
#   grupos_extra   mas grupos que la ven (sin /DEV/ ni /PRO/)
#   tema, logo     (principal) tema de Keycloak y logo de su portada en su dominio
#   redirigir      (principal) {ruta: destino} rutas viejas de su dominio
#   npm_antes      (principal) a donde iba su dominio antes (para volver atras)
CONSERVAR = ("dominio", "contenedores", "grupos_extra", "tema", "logo", "redirigir", "npm_antes")


def id_app(m: dict) -> str:
    """Una app se llama por su ruta; una subapp, principal.ruta (p. ej. spsil.tareas)."""
    return f"{m['dentro']}.{m['ruta']}" if m.get("dentro") else m["ruta"]


def _limpiar_extra(datos: dict) -> dict:
    """Lo guardado que no cuadra no se usa (lo puede haber tocado alguien a mano)."""
    cont = datos.get("contenedores") if isinstance(datos.get("contenedores"), dict) else {}
    datos["contenedores"] = {e: v for e, v in cont.items() if e in ENTORNOS and isinstance(v, str) and CONTENEDOR.match(v)}
    ge = datos.get("grupos_extra") if isinstance(datos.get("grupos_extra"), list) else []
    datos["grupos_extra"] = [g for g in ge if isinstance(g, str) and GRUPO.match(g)][:8]
    red = datos.get("redirigir") if isinstance(datos.get("redirigir"), dict) else {}
    datos["redirigir"] = {r: d for r, d in red.items() if RUTA.match(str(r)) and r not in ("ajustes", "portada")
                          and isinstance(d, str) and DESTINO.match(d)}
    datos["dominio"] = datos.get("dominio") if isinstance(datos.get("dominio"), str) and DOMINIO.match(datos.get("dominio") or "") else ""
    datos["tema"] = datos.get("tema") if isinstance(datos.get("tema"), str) and CONTENEDOR.match(datos.get("tema") or "") else ""
    datos["logo"] = datos.get("logo") if isinstance(datos.get("logo"), str) and ESTATICO.match(datos.get("logo") or "") else ""
    return datos


def registro() -> dict:
    """{id: datos} de las apps dadas de alta (registro/<id>.json)."""
    apps = {}
    for f in sorted((DIR / "registro").glob("*.json")):
        try:
            datos = json.loads(f.read_text(encoding="utf-8"))
            m = validar_manifiesto(datos.get("manifiesto", {}), datos.get("repo", ""))
        except (ValueError, Error) as e:
            log(f"aviso: {f.name} no vale ({e}); no se usa")
            continue
        if id_app(m) != f.stem:
            continue
        datos["manifiesto"] = m
        apps[f.stem] = _limpiar_extra(datos)
    # una subapp sin su principal no va a ningun sitio
    return {k: d for k, d in apps.items()
            if not d["manifiesto"].get("dentro") or apps.get(d["manifiesto"]["dentro"], {}).get("manifiesto", {}).get("principal")}


def guardar_registro(m: dict, repo: str = "", origen: str = "panel", **extra) -> None:
    ida = id_app(m)
    previo = {}
    try:
        previo = json.loads((DIR / "registro" / f"{ida}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    datos = {**{k: v for k, v in previo.items() if k in CONSERVAR}, **extra,
             "manifiesto": m, "repo": repo, "origen": origen, "fecha": ahora()}
    escribir(DIR / "registro" / f"{ida}.json", json.dumps(datos, ensure_ascii=False, indent=2) + "\n")


# ── donde va cada app en cada entorno ────────────────────────────────────────

def principal_de(reg: dict, datos: dict) -> dict | None:
    d = datos["manifiesto"].get("dentro")
    return reg.get(d) if d else None


def en_su_dominio(reg: dict, datos: dict, ent: str) -> str:
    """El dominio por el que se entra a la app en ese entorno, si no es el del entorno."""
    m = datos["manifiesto"]
    if ent != "pro" or not (m.get("principal") or m.get("dentro")):
        return ""   # una app suelta con dominio propio sigue tambien en pro.../<ruta>/
    p = principal_de(reg, datos)
    return (p or datos).get("dominio", "")


def camino(reg: dict, datos: dict, ent: str) -> str:
    """Su ruta: /tareas; una subapp, /spsil/tareas (o /tareas en el dominio de su principal)."""
    m = datos["manifiesto"]
    if en_su_dominio(reg, datos, ent):
        return "" if m.get("principal") else f"/{m['ruta']}"
    if m.get("dentro"):
        return f"/{m['dentro']}/{m['ruta']}"
    return f"/{m['ruta']}"


def contenedor_en(datos: dict, ent: str) -> str:
    m = datos["manifiesto"]
    return datos.get("contenedores", {}).get(ent) or nombre_de(m["contenedor"], m["ruta"], ent)


def grupos_app(c: dict, reg: dict, datos: dict, ent: str, suelto: str = "") -> list:
    """Los grupos (camino entero, sin la / del principio) que la ven en ese entorno.
    Igual para todo el servidor: una app, /DEV/<Grupo>; una subapp,
    /DEV/<Principal>/<Grupo> (y /PRO/... en PRO, este o no en su dominio)."""
    m = datos["manifiesto"]
    nombres = [g for g in dict.fromkeys([m["grupo"] or suelto, *datos.get("grupos_extra", [])]) if g and GRUPO.match(g)]
    p = principal_de(reg, datos)
    if p is None:
        return [f"{ent.upper()}/{g}" for g in nombres]
    pg = p["manifiesto"]["grupo"] or p["manifiesto"]["nombre"]
    pg = pg if GRUPO.match(pg) else p["manifiesto"]["ruta"]
    return [f"{ent.upper()}/{pg}/{g}" for g in nombres]


def subapps(reg: dict, ruta_principal: str) -> dict:
    return {k: d for k, d in reg.items() if d["manifiesto"].get("dentro") == ruta_principal}


# ── apps de la entrada de antes (las rutas que escribe cada app) ────────────
# Hasta que una app pase a su manifiesto, su .conf de la carpeta de antes
# (LEGADO/<ENT>/apps/*.conf, con "# app:", "# salud:" y "# grupo:") se copia
# tal cual a entrada/<ENT>/legado/.

def grupos_sueltos() -> dict:
    """ruta -> grupo (sin DEV/ ni PRO/), de 'servidor grupo' o de la portada de antes."""
    g = {}
    c = cfg()
    for f in ([legado(c) / "grupos"] if legado(c) else []) + [DIR / "grupos"]:
        if f.exists():
            for linea in f.read_text(encoding="utf-8").splitlines():
                partes = linea.split(None, 1)
                if len(partes) == 2 and RUTA.match(partes[0]) and GRUPO.match(partes[1].strip()):
                    g[partes[0]] = partes[1].strip()
    return g


def apps_legado(ent: str) -> list[dict]:
    c = cfg()
    base = legado(c)
    if not base:
        return []
    salida = []
    for f in sorted((base / ent / "apps").glob("*.conf")):
        ruta = f.stem
        if not RUTA.match(ruta) or ruta in RESERVADAS:
            continue
        texto = f.read_text(encoding="utf-8", errors="replace")
        meta = {k: (re.search(rf"^# {k}: *(.+)$", texto, re.M) or [None, ""])[1].strip() for k in ("app", "salud", "grupo")}
        grupo = meta["grupo"].strip("/") if re.fullmatch(r"[A-Za-z0-9/ _.-]*", meta["grupo"]) else ""
        salud = meta["salud"] if CAMINO.match(meta["salud"]) else ""
        salida.append({"ruta": ruta, "nombre": re.sub(r"[<>\"';|$]", "", meta["app"])[:60] or ruta,
                       "salud": (salud or "version/").lstrip("/"), "grupo": grupo, "texto": texto})
    return salida


# ── la entrada (nginx) de cada entorno ───────────────────────────────────────

CABECERAS = """  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
  proxy_set_header X-Forwarded-Host $host;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
"""


def conf_default(ent: str) -> str:
    """La portada (/) y Ajustes (/ajustes/) van al panel; X-Entorno la pone
    esta entrada (el navegador no puede cambiarla). Cada app, en su ruta."""
    return f"""# Entrada del entorno {ent.upper()} (la escribe servidor.py: no la cambies a mano)
server {{
  listen 80 default_server;
  client_max_body_size 50m;
  resolver 127.0.0.11 valid=10s ipv6=off;
  error_page 502 503 504 /no-disponible.html;
  location = /no-disponible.html {{ root /etc/nginx/conf.d/html; internal; }}
  location = / {{
    set $panel http://{PANEL}:8000;
    proxy_pass $panel/portada/;
{CABECERAS}    proxy_set_header X-Entorno {ent};
  }}
  location = /portada/ {{ return 302 /; }}
  location = /ajustes {{ return 308 /ajustes/; }}
  location /ajustes/ {{
    set $panel http://{PANEL}:8000;
    proxy_pass $panel;
{CABECERAS}    proxy_set_header X-Entorno {ent};
  }}
  include /etc/nginx/conf.d/apps/*.conf;
  include /etc/nginx/conf.d/legado/*.conf;
}}
""".replace("\n  proxy_", "\n    proxy_")


def conf_app(m: dict, ent: str, ruta: str = "", contenedor: str = "") -> str:
    """La app en su camino (ruta: 'tareas' o 'spsil/tareas')."""
    ruta = ruta or m["ruta"]
    destino = f"{contenedor or nombre_de(m['contenedor'], m['ruta'], ent)}:{m['puerto']}"
    quitar = f"  rewrite ^/{ruta}/(.*)$ /$1 break;\n" if m["prefijo"] == "quitar" else ""
    return f"""# app: {m['nombre']}
# creada-por: servidor
location = /{ruta} {{ return 308 /{ruta}/; }}
location /{ruta}/ {{
  set $destino {destino};
{quitar}  proxy_pass http://$destino;
{CABECERAS}  proxy_set_header X-Forwarded-Prefix /{ruta};
  proxy_read_timeout 300s;
}}
"""


def conf_principal(datos: dict, ent: str) -> str:
    """La portada de una app principal en /<ruta>/ (el panel, solo con sus subapps).
    Si en PRO ya tiene su dominio, /<ruta>/... lleva alli."""
    m = datos["manifiesto"]
    r = m["ruta"]
    if ent == "pro" and datos.get("dominio"):
        return f"""# app principal: {m['nombre']} (en su dominio)
location = /{r} {{ return 301 https://{datos['dominio']}/; }}
location /{r}/ {{ rewrite ^/{r}/(.*)$ https://{datos['dominio']}/$1 permanent; }}
"""
    return f"""# app principal: {m['nombre']}
location = /{r} {{ return 308 /{r}/; }}
location = /{r}/ {{
  set $panel http://{PANEL}:8000;
  proxy_pass $panel/portada/;
{CABECERAS}  proxy_set_header X-Entorno {ent};
  proxy_set_header X-Principal {r};
}}
"""


def conf_dominio_principal(reg: dict, datos: dict) -> str:
    """El dominio de una app principal (PRO): su portada en /, sus subapps en
    /<ruta>/, el login de su portada, sus ficheros fijos y sus rutas viejas."""
    m = datos["manifiesto"]
    r, dom = m["ruta"], datos["dominio"]
    def panel(camino: str, destino: str, reescribir: str = "") -> str:
        # con variable en proxy_pass, una URI ahi sustituye la peticion entera:
        # para un prefijo (/marca/) se reescribe antes
        return f"""  location {camino} {{
    set $panel http://{PANEL}:8000;
{reescribir}    proxy_pass $panel{destino};
{CABECERAS}    proxy_set_header X-Entorno pro;
    proxy_set_header X-Principal {r};
  }}
"""
    partes = [panel("= /", "/portada/"), "  location = /portada/ { return 302 /; }\n",
              "  location = /ajustes { return 308 /ajustes/; }\n", panel("/ajustes/", "")]
    for f in estaticos(r):
        partes.append(panel(f"= /{f}", f"/ajustes/estaticos/{r}/{f}"))
    for carpeta in CARPETAS_FIJAS:
        if (DIR / "estado" / "estaticos" / r / carpeta).is_dir():
            partes.append(panel(f"/{carpeta}/", "", f"    rewrite ^/{carpeta}/(.*)$ /ajustes/estaticos/{r}/{carpeta}/$1 break;\n"))
    for ruta, dest in sorted(datos.get("redirigir", {}).items()):
        if re.fullmatch(r"https://[a-z0-9.-]+", dest):   # a otro dominio: con el camino entero (/auth/... -> users.../auth/...)
            partes.append(f"  location /{ruta} {{ return 301 {dest}$request_uri; }}\n")
        else:
            partes.append(f"  location = /{ruta} {{ return 301 {dest}; }}\n  location /{ruta}/ {{ return 301 {dest}; }}\n")
    for sub in sorted(subapps(reg, r).values(), key=lambda d: d["manifiesto"]["ruta"]):
        sm = sub["manifiesto"]
        partes.append("".join("  " + l + "\n" if l else "\n"
                              for l in conf_app(sm, "pro", sm["ruta"], contenedor_en(sub, "pro")).splitlines()))
    return f"""# {m['nombre']} de PRO en su dominio (servidor.py)
server {{
  listen 80;
  server_name {dom};
  client_max_body_size 50m;
  resolver 127.0.0.11 valid=10s ipv6=off;
  error_page 502 503 504 /no-disponible.html;
  location = /no-disponible.html {{ root /etc/nginx/conf.d/html; internal; }}
{"".join(partes)}}}
""".replace("\n  proxy_", "\n    proxy_")


def estaticos(ruta: str) -> list:
    """Los ficheros fijos (logo...) de la portada de una app principal: estado/estaticos/<ruta>/."""
    base = DIR / "estado" / "estaticos" / ruta
    return sorted(f.name for f in base.glob("*") if f.is_file() and ESTATICO.match(f.name)) if base.is_dir() else []


def conf_dominio(m: dict, dom: str) -> str:
    """Un dominio propio (solo PRO): la app entera en / de ese dominio."""
    destino = f"{nombre_de(m['contenedor'], m['ruta'], 'pro')}:{m['puerto']}"
    return f"""# {m['nombre']} en su dominio propio (servidor.py)
server {{
  listen 80;
  server_name {dom};
  client_max_body_size 50m;
  resolver 127.0.0.11 valid=10s ipv6=off;
  error_page 502 503 504 /no-disponible.html;
  location = /no-disponible.html {{ root /etc/nginx/conf.d/html; internal; }}
  location / {{
    set $destino {destino};
    proxy_pass http://$destino;
{CABECERAS}    proxy_set_header X-Forwarded-Prefix "";
    proxy_read_timeout 300s;
  }}
}}
""".replace("\n  proxy_", "\n    proxy_")


def pagina_no_disponible(ent: str) -> str:
    return f"""<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>No disponible · {ent.upper()}</title>
<style>
:root{{--fondo:#f4f6fb;--texto:#111827;--suave:#6b7280;--acento:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--fondo:#0b1020;--texto:#e5e7eb;--suave:#9ca3af;--acento:#60a5fa}}}}
body{{margin:0;min-height:100vh;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--fondo);color:var(--texto)}}
main{{max-width:520px;margin:12vh auto 0;padding:0 20px;text-align:center}}
.ico{{margin:0 auto 20px;width:56px;height:56px;border-radius:14px;display:grid;place-items:center;font-weight:700;font-size:26px;color:#fff;background:linear-gradient(135deg,#2563eb,#7c3aed)}}
p{{color:var(--suave)}} a{{color:var(--acento)}}
.boton{{display:inline-block;margin-top:16px;padding:10px 18px;border-radius:10px;background:var(--acento);color:#fff;text-decoration:none;font-weight:600}}
</style>
<main><div class="ico">!</div><h1>Esta aplicación no está disponible</h1>
<p>Puede que se esté actualizando o que aún no tenga ninguna versión en {ent.upper()}. Prueba otra vez en unos segundos.</p>
<a class="boton" href="javascript:location.reload()">Reintentar</a><p><a href="/">Volver al inicio</a></p></main></html>
"""


def ficheros_entrada(ent: str) -> dict:
    """Todo lo que lleva la carpeta entrada/<ENT>/: {camino relativo: texto}."""
    reg = registro()
    f = {"default.conf": conf_default(ent), "html/no-disponible.html": pagina_no_disponible(ent)}
    for ida, datos in reg.items():
        m = datos["manifiesto"]
        if m.get("principal"):
            f[f"apps/{ida}.conf"] = conf_principal(datos, ent)
            if ent == "pro" and datos.get("dominio"):
                f[f"dominio-{ida}.conf"] = conf_dominio_principal(reg, datos)
        elif m.get("dentro"):
            if not en_su_dominio(reg, datos, ent):   # si no, va en el server del dominio
                f[f"apps/{ida}.conf"] = conf_app(m, ent, camino(reg, datos, ent).lstrip("/"), contenedor_en(datos, ent))
        else:
            if m["entrada"] == "servidor":
                f[f"apps/{ida}.conf"] = conf_app(m, ent, contenedor=contenedor_en(datos, ent))
            if ent == "pro" and datos.get("dominio"):
                f[f"dominio-{ida}.conf"] = conf_dominio({**m, "contenedor": contenedor_en(datos, "pro")}, datos["dominio"])
    propias_servidor = {r for r, d in reg.items() if d["manifiesto"]["entrada"] == "servidor"}
    for a in apps_legado(ent):
        if a["ruta"] not in propias_servidor:
            f[f"legado/{a['ruta']}.conf"] = a["texto"]
    return f


def _leer_carpeta(base: Path) -> dict:
    return {str(p.relative_to(base)): p.read_text(encoding="utf-8", errors="replace")
            for p in base.rglob("*") if p.is_file()}


def _volcar(base: Path, ficheros: dict) -> None:
    """Deja la carpeta exactamente con esos ficheros (sin cambiar la carpeta
    en si: nginx la tiene montada)."""
    base.mkdir(parents=True, exist_ok=True)
    for sub in ("apps", "legado", "html"):
        (base / sub).mkdir(exist_ok=True)
    for p in list(base.rglob("*")):
        if p.is_file() and str(p.relative_to(base)) not in ficheros:
            p.unlink()
    for rel, texto in ficheros.items():
        escribir(base / rel, texto)


def nginx_valida(base: Path) -> None:
    """nginx -t con esa carpeta, en un contenedor de usar y tirar. En la red
    del proxy si existe: una ruta de antes puede nombrar su contenedor sin
    variable y nginx lo busca al arrancar."""
    red = cfg()["RED_PROXY"]
    if subprocess.run(["docker", "network", "inspect", red], capture_output=True).returncode != 0:
        red = "none"
    run(["docker", "run", "--rm", "--network", red, "-v", f"{base}:/etc/nginx/conf.d:ro",
         IMAGEN_NGINX, "nginx", "-t", "-q"], timeout=120)


_RECHAZADAS: dict = {}   # entorno -> configuracion que nginx no acepto (para no reintentarla cada 3 s)


def aplicar_entrada(ent: str, forzar_recarga: bool = False) -> bool:
    """Pone la configuracion que toca; si nginx no la acepta, deja la de antes."""
    base = DIR / "entrada" / ent
    nuevos = ficheros_entrada(ent)
    antes = _leer_carpeta(base) if base.exists() else {}
    if antes == nuevos and not forzar_recarga:
        return False
    huella = hash(json.dumps(nuevos, sort_keys=True))
    if _RECHAZADAS.get(ent) == huella and not forzar_recarga:
        return False   # ya se probo y nginx no la quiso: hasta que cambie algo
    _volcar(base, nuevos)
    try:
        nginx_valida(base)
    except Error as e:
        _volcar(base, antes)
        _RECHAZADAS[ent] = huella
        raise Error(f"{ent}: nginx no acepta la configuración nueva; sigue la de antes.\n{e}") from None
    _RECHAZADAS.pop(ent, None)
    subprocess.run(["docker", "exec", f"entrada-{ent}", "nginx", "-s", "reload"], capture_output=True)
    log(f"{ent}: entrada actualizada")
    return True


def compose_entrada(c: dict) -> Path:
    servicios = "".join(f"""  {ent}:
    image: {IMAGEN_NGINX}
    container_name: entrada-{ent}
    restart: unless-stopped
    mem_limit: 64m
    volumes:
      - ./{ent}:/etc/nginx/conf.d:ro
    networks:
      proxy:
        aliases: [entrada-{ent}]
""" for ent in c["_gestionados"])
    p = DIR / "entrada" / "docker-compose.yml"
    escribir(p, f"""# Entradas de los entornos que lleva el servidor (lo escribe servidor.py)
name: servidor-entrada
services:
{servicios or '  {}'}
networks:
  proxy:
    name: {c['RED_PROXY']}
    external: true
""")
    return p


# ── catalogo para el panel ───────────────────────────────────────────────────

def catalogo() -> dict:
    c = cfg()
    reg = registro()
    sueltos = grupos_sueltos()
    entradas = {}
    for ent in ENTORNOS:
        lista, vistas = [], set()
        legado_ent = {a["ruta"]: a for a in apps_legado(ent)}
        for ida, datos in reg.items():
            m = datos["manifiesto"]
            if not m.get("dentro") and not m.get("principal") and m["entrada"] == "propia" and m["ruta"] not in legado_ent:
                continue   # su ruta aun no existe en este entorno
            gs = grupos_app(c, reg, datos, ent, "" if m.get("dentro") else sueltos.get(m["ruta"], ""))
            dom = en_su_dominio(reg, datos, ent)
            lista.append({"ruta": m["ruta"], "id": ida, "nombre": m["nombre"], "salud": m["version"] or "version/",
                          "grupo": gs[0] if gs else "", "grupos": gs, "camino": camino(reg, datos, ent) + "/",
                          "host": dom or dominio(c, ent), "dentro": m.get("dentro", ""),
                          "principal": bool(m.get("principal"))})
            vistas.add(m["ruta"])
        for ruta, a in legado_ent.items():
            if ruta in vistas:
                continue
            # siempre bajo el entorno: un "# grupo: Lin-AI" suelto no puede valer
            # como el grupo raiz /Lin-AI (otro sistema) ni el de PRO en DEV
            g = a["grupo"] or sueltos.get(ruta, "")
            for pre in ("DEV/", "PRO/"):
                g = g.removeprefix(pre)
            grupo = f"{ent.upper()}/{g}" if g else ""
            lista.append({"ruta": ruta, "id": ruta, "nombre": a["nombre"], "salud": a["salud"], "grupo": grupo,
                          "grupos": [grupo] if grupo else [], "camino": f"/{ruta}/", "host": dominio(c, ent),
                          "dentro": "", "principal": False})
        entradas[ent] = sorted(lista, key=lambda x: x["nombre"].lower())
    apps = []
    for ida, datos in sorted(reg.items()):
        m = datos["manifiesto"]
        extra = {}
        if m["despliegue"]["tipo"] == "servidor":
            extra["versiones"] = {e: leer_env(_dir_app(ida, e) / ".env").get("VERSION", "") for e in ENTORNOS}
        apps.append({**m, "id": ida, "dentro": m.get("dentro", ""), "principal": bool(m.get("principal")),
                     "repo": datos.get("repo", ""), "origen": datos.get("origen", ""), "dominio": datos.get("dominio", ""),
                     "grupos_extra": datos["grupos_extra"], "contenedores": {e: contenedor_en(datos, e) for e in ENTORNOS},
                     "tema": datos.get("tema", ""), "logo": datos.get("logo", ""), "redirigir": datos.get("redirigir", {}),
                     "donde": {e: {"host": en_su_dominio(reg, datos, e) or dominio(c, e), "camino": camino(reg, datos, e) + "/"}
                               for e in ENTORNOS},
                     "estado_dominio": leer_estado_dominio(ida) if datos.get("dominio") else {}, **extra})
    sin_alta = sorted({a["ruta"] for e in ENTORNOS for a in apps_legado(e)} - set(reg))
    return {"version": version_actual(), "latido": ahora(),
            "dominios": {e: dominio(c, e) for e in ENTORNOS}, "gestionados": c["_gestionados"],
            "ajustes_en": c["AJUSTES_EN"], "grupo_it": c["GRUPO_IT"], "entradas": entradas,
            "apps": apps, "sin_alta": [{"ruta": r, "grupo": sueltos.get(r, "")} for r in sin_alta],
            "npm": (DIR / ".npm.env").exists(), "cloudflare": (DIR / ".cloudflare.env").exists()}


def escribir_catalogo() -> None:
    datos = catalogo()
    previo = {}
    try:
        previo = json.loads((DIR / "estado" / "catalogo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    # el latido cambia siempre: se reescribe a lo sumo cada 30 s si no hay otra cosa
    sin = lambda d: {k: v for k, v in d.items() if k != "latido"}   # noqa: E731
    viejo = previo.get("latido", "")
    if sin(previo) == sin(datos) and viejo and viejo > _hace(30):
        return
    escribir(DIR / "estado" / "catalogo.json", json.dumps(datos, ensure_ascii=False, indent=1) + "\n")


def _hace(segundos: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=segundos)).strftime("%Y-%m-%dT%H:%M:%SZ")


def version_actual() -> str:
    try:
        return run(["git", "-C", str(DIR / "codigo"), "rev-parse", "--short=7", "HEAD"]).strip()
    except (Error, FileNotFoundError):
        return ""


# ── Keycloak (kcadm dentro de su contenedor) ─────────────────────────────────

def _realm(c: dict) -> str:
    issuer = c.get("KEYCLOAK_ISSUER", "")
    if "/realms/" not in issuer:
        raise Error("falta KEYCLOAK_ISSUER en servidor.env")
    return issuer.rstrip("/").rsplit("/", 1)[-1]


def kc(*args, entrada: str | None = None) -> str:
    return run(["docker", "exec", "-i", cfg()["KEYCLOAK_CONTENEDOR"], "/opt/keycloak/bin/kcadm.sh", *args],
               entrada=entrada, timeout=60)


def kc_entrar() -> None:
    try:
        run(["docker", "exec", cfg()["KEYCLOAK_CONTENEDOR"], "sh", "-c",
             '/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080/auth --realm master '
             '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD"'], timeout=60)
    except Error as e:
        raise Error(f"no puedo entrar en Keycloak como admin: {e}") from None


def kc_grupo(realm: str, padre: str, nombre: str) -> str:
    """id del grupo 'nombre' (dentro de 'padre', o arriba del todo); lo crea si falta."""
    lista = f"groups/{padre}/children" if padre else "groups"
    for fila in kc("get", lista, "-r", realm, "--fields", "id,name", "--format", "csv", "--noquotes").splitlines():
        gid, _, n = fila.strip().partition(",")
        if n == nombre:
            return gid
    gid = kc("create", lista, "-r", realm, "-s", f"name={nombre}", "-i").strip()
    log(f"Keycloak: grupo {nombre} creado")
    return gid


def asegurar_grupos(grupo: str) -> None:
    """/DEV/<grupo> y /PRO/<grupo>, cada uno con su /Admin. Si ya estan, nada."""
    asegurar_caminos([f"{ent.upper()}/{grupo}" for ent in ENTORNOS])


def asegurar_caminos(caminos: list) -> None:
    """Cada grupo (p. ej. DEV/SPSIL/Tareas, con los de por medio) con su /Admin."""
    realm = _realm(cfg())
    for cam in caminos:
        gid = ""
        for parte in cam.strip("/").split("/"):
            gid = kc_grupo(realm, gid, parte)
        kc_grupo(realm, gid, "Admin")


def _cliente_id(realm: str, cliente: str) -> str:
    filas = kc("get", "clients", "-r", realm, "-q", f"clientId={cliente}", "--fields", "id",
               "--format", "csv", "--noquotes").split()
    return filas[0].strip() if filas else ""


def asegurar_cliente(cliente: str, nombre: str, urls: list[str], tema: str = "") -> str:
    """El cliente (confidencial, con el mapper de grupos con su camino entero)
    y que admita volver a esas direcciones. Con tema, el de su login. Devuelve su secret."""
    c = cfg()
    realm = _realm(c)
    cid = _cliente_id(realm, cliente) or ""
    if not cid:
        cuerpo = {
            "clientId": cliente, "name": nombre, "enabled": True, "publicClient": False,
            "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
            "redirectUris": [], "webOrigins": [], "attributes": {"login_theme": tema or "keycloak"},
            "protocolMappers": [{"name": "groups", "protocol": "openid-connect",
                                 "protocolMapper": "oidc-group-membership-mapper",
                                 "config": {"claim.name": "groups", "full.path": "true", "userinfo.token.claim": "true",
                                            "id.token.claim": "true", "access.token.claim": "true"}}]}
        cid = kc("create", "clients", "-r", realm, "-i", "-f", "-", entrada=json.dumps(cuerpo)).strip()
        log(f"Keycloak: cliente {cliente} creado")
    elif tema:
        cli = json.loads(kc("get", f"clients/{cid}", "-r", realm))
        if (cli.get("attributes") or {}).get("login_theme") != tema:
            cli.setdefault("attributes", {})["login_theme"] = tema
            kc("update", f"clients/{cid}", "-r", realm, "-f", "-", entrada=json.dumps(cli))
            log(f"Keycloak: {cliente} con el login {tema}")
    for u in urls:
        anadir_url_cliente(realm, cid, u)
    return kc("get", f"clients/{cid}/client-secret", "-r", realm, "--fields", "value",
              "--format", "csv", "--noquotes").strip()


def anadir_url_cliente(realm: str, cid: str, url: str) -> None:
    """Vuelta del login, origen y vuelta del cierre de sesion para url (https://host[/ruta])."""
    from urllib.parse import urlparse
    url = url.rstrip("/")
    cli = json.loads(kc("get", f"clients/{cid}", "-r", realm))
    p = urlparse(url)
    origen = f"{p.scheme}://{p.netloc}"
    antes = json.dumps(cli, sort_keys=True)
    cli["redirectUris"] = sorted(set(cli.get("redirectUris") or []) | {url + "/*"})
    cli["webOrigins"] = sorted(set(cli.get("webOrigins") or []) | {origen})
    a = cli.setdefault("attributes", {})
    v = [x for x in (a.get("post.logout.redirect.uris") or "").split("##") if x]
    a["post.logout.redirect.uris"] = "##".join(sorted(set(v) | {origen + "/*"}))
    if json.dumps(cli, sort_keys=True) != antes:
        kc("update", f"clients/{cid}", "-r", realm, "-f", "-", entrada=json.dumps(cli))
        log(f"Keycloak: {url} admitida en {cli.get('clientId')}")


# ── Nginx Proxy Manager (dominios propios) ───────────────────────────────────

def _npm_url() -> str:
    u = leer_env(DIR / ".npm.env").get("NPM_URL", "")
    if u:
        return u
    for fila in run(["docker", "ps", "--format", "{{.ID}} {{.Image}}"]).splitlines():
        cid, _, imagen = fila.partition(" ")
        if "nginx-proxy-manager" in imagen:
            ip = run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", cid]).split()
            if ip:
                return f"http://{ip[0]}:81"
    raise Error("no encuentro Nginx Proxy Manager")


def _npm(metodo: str, ruta: str, datos=None, token: str = ""):
    cab = {"Content-Type": "application/json"}
    if token:
        cab["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(_npm_url() + ruta, method=metodo, headers=cab,
                               data=json.dumps(datos).encode() if datos is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise Error(f"Nginx Proxy Manager respondió {e.code}: {e.read()[:300].decode(errors='replace')}") from None
    except urllib.error.URLError as e:
        raise Error(f"no llego a Nginx Proxy Manager: {e}") from None


def npm_token() -> str:
    a = leer_env(DIR / ".npm.env")
    if not a.get("NPM_EMAIL"):
        raise Error("falta el acceso a Nginx Proxy Manager (Ajustes → Acceso a Nginx Proxy Manager)")
    return _npm("POST", "/api/tokens", {"identity": a["NPM_EMAIL"], "secret": a.get("NPM_CLAVE", "")})["token"]


def npm_host(dom: str, token: str = "") -> dict:
    for h in _npm("GET", "/api/nginx/proxy-hosts", token=token or npm_token()):
        if dom in h.get("domain_names", []):
            return h
    return {}


def npm_apuntar(dom: str, host: str, puerto: int) -> dict:
    """Que dom vaya a host:puerto (lo crea con certificado si no esta). Devuelve
    a donde iba antes ({host, puerto}; vacio si no estaba)."""
    token = npm_token()
    h = npm_host(dom, token)
    if not h:
        npm_proxy_host(dom, host, puerto)
        return {}
    antes = {"host": h.get("forward_host", ""), "puerto": int(h.get("forward_port") or 80)}
    if (antes["host"], antes["puerto"]) != (host, puerto):
        _npm("PUT", f"/api/nginx/proxy-hosts/{int(h['id'])}", {"forward_host": host, "forward_port": puerto}, token=token)
        log(f"{dom}: en Nginx Proxy Manager ahora va a {host}:{puerto}")
    return antes


def npm_proxy_host(dom: str, host: str, puerto: int) -> None:
    token = npm_token()
    if npm_host(dom, token):
        log(f"{dom} ya estaba en Nginx Proxy Manager")
        return
    _npm("POST", "/api/nginx/proxy-hosts", {
        "domain_names": [dom], "forward_scheme": "http", "forward_host": host, "forward_port": puerto,
        "certificate_id": "new", "ssl_forced": True, "http2_support": True, "block_exploits": True,
        "allow_websocket_upgrade": True, "access_list_id": 0, "caching_enabled": False,
        "hsts_enabled": False, "hsts_subdomains": False, "locations": [],
        "advanced_config": "proxy_buffer_size 128k;\nproxy_buffers 4 256k;",
        "meta": {"letsencrypt_agree": True, "letsencrypt_email": leer_env(DIR / ".npm.env")["NPM_EMAIL"],
                 "dns_challenge": False}}, token=token)
    log(f"{dom}: creado en Nginx Proxy Manager, con certificado")


# ── Cloudflare (DNS de los dominios propios, si se guarda su token) ─────────

def _cf(metodo: str, ruta: str, datos=None, token: str = ""):
    tok = token or leer_env(DIR / ".cloudflare.env").get("CF_TOKEN", "")
    if not tok:
        raise Error("falta el token de Cloudflare (Ajustes → Cloudflare)")
    r = urllib.request.Request("https://api.cloudflare.com/client/v4" + ruta, method=metodo,
                               headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                               data=json.dumps(datos).encode() if datos is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            d = json.load(resp)
    except urllib.error.HTTPError as e:
        raise Error(f"Cloudflare respondió {e.code}: {e.read()[:300].decode(errors='replace')}") from None
    except urllib.error.URLError as e:
        raise Error(f"no llego a Cloudflare: {e}") from None
    if not d.get("success"):
        raise Error(f"Cloudflare: {str(d.get('errors'))[:300]}")
    return d.get("result")


def cloudflare_registro(dom: str, ip: str) -> None:
    """El registro A de dom hacia ip, sin proxy (nube gris). Si ya hay uno que
    va a otro sitio, no se cambia: se avisa."""
    if not ip:
        raise Error("no sé la IP del servidor (pon SERVIDOR_IP en servidor.env)")
    partes = dom.split(".")
    zona = None
    for i in range(len(partes) - 1):
        z = _cf("GET", f"/zones?name={'.'.join(partes[i:])}")
        if z:
            zona = z[0]
            break
    if not zona:
        raise Error(f"el token de Cloudflare no ve la zona de {dom}")
    regs = _cf("GET", f"/zones/{zona['id']}/dns_records?name={dom}") or []
    a = [x for x in regs if x.get("type") == "A"]
    if not regs:
        _cf("POST", f"/zones/{zona['id']}/dns_records", {"type": "A", "name": dom, "content": ip, "proxied": False, "ttl": 1})
        log(f"{dom}: registro DNS creado hacia {ip} (nube gris)")
        return
    if a and a[0].get("content") == ip:
        if a[0].get("proxied"):
            log(f"aviso: {dom} tiene la nube naranja en Cloudflare: el certificado necesita la gris")
        else:
            log(f"{dom}: DNS ya bien ({ip})")
        return
    log(f"aviso: {dom} ya tiene en Cloudflare un registro que va a otro sitio ({(regs[0].get('type'))} {regs[0].get('content')}); no lo cambio")


def _acc_cloudflare(d: dict) -> None:
    tok = str(d.get("token", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,200}", tok):
        raise Error("eso no parece un token de Cloudflare")
    _cf("GET", "/user/tokens/verify", token=tok)
    poner_env(DIR / ".cloudflare.env", {"CF_TOKEN": tok})
    log("token de Cloudflare guardado (solo lo lee el servidor)")


# ── apps que despliega el servidor (despliegue.tipo = servidor) ──────────────

def _dir_app(ruta: str, ent: str) -> Path:
    return DIR / "apps" / ruta / ent


def _compose_app(m: dict, ent: str, ida: str, contenedor: str) -> None:
    d = _dir_app(ida, ent)
    escribir(d / "docker-compose.yml", f"""# {m['nombre']} en {ent.upper()} (lo escribe servidor.py)
services:
  app:
    image: ${{IMAGEN:?falta IMAGEN}}:${{VERSION:?falta VERSION}}
    restart: unless-stopped
    env_file:
      - path: app.env
        required: false
    mem_limit: {m['despliegue']['memoria']}m
    volumes:
      - datos:/datos
    networks:
      proxy:
        aliases: [{contenedor}]
volumes:
  datos:
networks:
  proxy:
    name: {cfg()['RED_PROXY']}
    external: true
""")


def _dca(ruta: str, ent: str, *args) -> str:
    d = _dir_app(ruta, ent)
    return run(["docker", "compose", "--project-directory", str(d), "-p", f"{ruta.replace('.', '-')}-{ent}",
                "-f", str(d / "docker-compose.yml"), *args], timeout=900)


def app_responde(m: dict, ent: str, host: str = "", camino_app: str = "") -> bool:
    c = cfg()
    r = subprocess.run(["docker", "exec", f"entrada-{ent}", "wget", "-q", "-T", "5", "-O", "/dev/null",
                        "--header", f"Host: {host or dominio(c, ent) or 'localhost'}",
                        f"http://127.0.0.1{camino_app or '/' + m['ruta']}/{m['version']}"], capture_output=True)
    return r.returncode == 0


def version_anterior(hist: Path, actual: str) -> str:
    """La ultima desplegada con exito que no sea la actual ni una de la que ya se volvio atras."""
    orden, mala = [], set()
    for linea in hist.read_text().splitlines():
        p = linea.split("\t")
        if len(p) >= 3:
            if p[2] == "ok":
                orden.append(p[1])
            elif p[2] in ("revertida", "fallo"):
                mala.add(p[1])
    for v in reversed(orden):
        if v != actual and v not in mala:
            return v
    return ""


def desplegar_app(ida: str, ent: str, version: str, origen: str = "panel") -> None:
    reg = registro()
    if ida not in reg or reg[ida]["manifiesto"]["despliegue"]["tipo"] != "servidor":
        raise Error(f"'{ida}' no es una app que despliegue el servidor")
    if ent not in ENTORNOS:
        raise Error("entorno no válido")
    datos = reg[ida]
    m = datos["manifiesto"]
    ruta = m["ruta"]
    host = en_su_dominio(reg, datos, ent) or ""
    cam = camino(reg, datos, ent)
    c = cfg()
    d = _dir_app(ida, ent)
    d.mkdir(parents=True, exist_ok=True)
    hist = d / "historial.log"
    hist.touch()
    actual = leer_env(d / ".env").get("VERSION", "")
    volver_de = ""
    if version == "anterior":
        version, origen, volver_de = version_anterior(hist, actual), "rollback", actual
        if not version:
            raise Error(f"no hay una versión anterior buena de {ruta} en {ent}")
    if not VERSION.match(version):
        raise Error(f"versión no válida: '{version}'")
    imagen = m["despliegue"]["imagen"]
    # su login: cliente <ruta>-<ent> en Keycloak, grupo <ENT>/<grupo> (o /<ENT>/<Principal>/<grupo>)
    base = f"https://{host or dominio(c, ent)}{cam}"
    gs = grupos_app(c, reg, datos, ent, ruta.capitalize())
    entorno_app = {"SERVIDOR_ENTORNO": ent, "APP_URL_PUBLICA": base, "APP_RUTA_BASE": cam,
                   "KEYCLOAK_ISSUER": c.get("KEYCLOAK_ISSUER", ""),
                   "KEYCLOAK_GRUPO_APP": gs[0] if gs else f"{ent.upper()}/{ruta.capitalize()}",
                   "KEYCLOAK_GRUPO_ADMIN_GLOBAL": c["GRUPO_IT"]}
    cliente = nombre_de(m["keycloak"].get("cliente", ""), ruta, ent)
    if cliente:
        secreto = leer_env(DIR / "secretos" / f"{cliente}.env").get("KEYCLOAK_CLIENT_SECRET", "")
        entorno_app.update({"KEYCLOAK_CLIENT_ID": cliente, "KEYCLOAK_CLIENT_SECRET": secreto, "AUTH_MODO": "sso"})
    poner_env(d / "app.env", entorno_app)
    poner_env(d / ".env", {"IMAGEN": imagen, "VERSION": version})
    _compose_app(m, ent, ida, contenedor_en(datos, ent))
    log(f"{ruta} {ent}: {imagen}:{version} (ahora: {actual or 'nada'})")
    if subprocess.run(["docker", "pull", "-q", f"{imagen}:{version}"], capture_output=True).returncode != 0:
        if subprocess.run(["docker", "image", "inspect", f"{imagen}:{version}"], capture_output=True).returncode != 0:
            poner_env(d / ".env", {"VERSION": actual})
            raise Error(f"no se puede descargar {imagen}:{version} (¿existe? si es privada, el servidor necesita "
                        "docker login ghcr.io)")
    _dca(ruta, ent, "up", "-d", "--remove-orphans")
    t = 0
    while t < ESPERA_SALUD:
        if app_responde(m, ent, host, cam):
            with hist.open("a") as h:
                h.write(f"{ahora()}\t{version}\tok\t{origen}\n")
            log(f"{ruta} {ent} ya está en {version}")
            if volver_de and volver_de != version:
                with hist.open("a") as h:   # para que otro "anterior" no vuelva a ella
                    h.write(f"{ahora()}\t{volver_de}\trevertida\t-> {version}\n")
            return
        time.sleep(5)
        t += 5
    with hist.open("a") as h:
        h.write(f"{ahora()}\t{version}\tfallo\t{origen}\n")
    if actual and actual != version:
        poner_env(d / ".env", {"VERSION": actual})
        _dca(ruta, ent, "up", "-d", "--remove-orphans")
        raise Error(f"la versión {version} de {ruta} no responde: sigue {actual}")
    raise Error(f"la versión {version} de {ruta} no responde")


# ── lo que pide el panel ─────────────────────────────────────────────────────

def _grupos_extra(valor) -> list:
    if isinstance(valor, str):
        valor = valor.replace("\n", ",").split(",")   # los grupos pueden llevar espacios
    lista = [str(g).strip().strip("/") for g in (valor or []) if str(g).strip()]
    malos = [g for g in lista if not GRUPO.match(g)]
    if malos:
        raise Error(f"grupo no válido: '{malos[0]}'")
    return list(dict.fromkeys(lista))[:8]


def _acc_registrar(d: dict) -> None:
    repo = str(d.get("repo", "")).strip()
    if repo and not REPO.match(repo):
        raise Error("repositorio no válido")
    man = d.get("manifiesto")
    if isinstance(man, dict):
        man = dict(man)
        dentro = str(d.get("dentro", "") or "").strip()
        if dentro:
            man["dentro"] = dentro
            man.pop("principal", None)
    m = validar_manifiesto(man, repo)
    reg = registro()
    if m.get("dentro") and not reg.get(m["dentro"], {}).get("manifiesto", {}).get("principal"):
        raise Error(f"/{m['dentro']}/ no es una app principal (créala antes)")
    ida = id_app(m)
    otro = reg.get(ida)
    if otro and otro.get("repo") and repo and otro["repo"] != repo:
        raise Error(f"{ida} ya es de {otro['repo']}: quítala antes si quieres cambiarla")
    extra = {}
    if "grupos_extra" in d:
        extra["grupos_extra"] = _grupos_extra(d["grupos_extra"])
    conts = {}
    for ent in ENTORNOS:
        v = str(d.get(f"contenedor_{ent}", "") or "").strip()
        if v and not CONTENEDOR.match(v):
            raise Error(f"contenedor de {ent.upper()} no válido: '{v}'")
        if v:
            conts[ent] = v
    if conts:
        extra["contenedores"] = conts
    guardar_registro(m, repo, origen=str(d.get("origen", "panel"))[:20], **extra)
    log(f"{ida} dada de alta: {m['nombre']}" + (f" (dentro de /{m['dentro']}/)" if m.get("dentro") else ""))
    _keycloak_de(ida)
    aplicar_todo()


def _keycloak_de(ida: str) -> None:
    """Sus grupos en DEV y PRO (con Admin) y, si lo dice su manifiesto, su cliente."""
    c = cfg()
    reg = registro()
    datos = reg.get(ida)
    if not datos:
        return
    m = datos["manifiesto"]
    try:
        kc_entrar()
        caminos = [g for ent in ENTORNOS for g in grupos_app(c, reg, datos, ent)]
        if caminos:
            asegurar_caminos(caminos)
            log("grupos " + ", ".join(f"/{g}" for g in caminos) + " (con Admin) listos")
        cliente = m["keycloak"].get("cliente", "")
        for ent in ENTORNOS if cliente else ():
            nombre = nombre_de(cliente, m["ruta"], ent)
            host = en_su_dominio(reg, datos, ent) or dominio(c, ent)
            urls = [f"https://{host}{camino(reg, datos, ent)}"] if host else []
            secreto = asegurar_cliente(nombre, f"{m['nombre']} ({ent.upper()})", urls)
            f = DIR / "secretos" / f"{nombre}.env"
            if not f.exists():
                f.parent.mkdir(mode=0o700, exist_ok=True)
                poner_env(f, {"KEYCLOAK_CLIENT_ID": nombre, "KEYCLOAK_CLIENT_SECRET": secreto})
                log(f"secret de {nombre} guardado en {f}")
    except Error as e:
        log(f"aviso: Keycloak: {e}")


def _id_de(d: dict) -> str:
    ida = str(d.get("id") or d.get("ruta") or "")
    if not ID_APP.match(ida):
        raise Error("esa app no está dada de alta")
    return ida


def _acc_quitar(d: dict) -> None:
    ida = _id_de(d)
    p = DIR / "registro" / f"{ida}.json"
    if not p.exists():
        raise Error("esa app no está dada de alta")
    reg = registro()
    if ida in reg and subapps(reg, ida):
        raise Error("tiene subapps: quítalas antes")
    if reg.get(ida, {}).get("dominio") and reg[ida]["manifiesto"].get("principal"):
        raise Error("está en su dominio: quítale antes el dominio")
    p.unlink()
    log(f"{ida} quitada del registro (sus contenedores y su grupo siguen como estaban)")
    aplicar_todo()


def _acc_grupo(d: dict) -> None:
    ida, grupo = str(d.get("id") or d.get("ruta") or ""), str(d.get("grupo", "")).strip().strip("/")
    for pre in ("DEV/", "PRO/"):
        grupo = grupo.removeprefix(pre)
    if not ID_APP.match(ida) or not GRUPO.match(grupo):
        raise Error("app o grupo no válidos (p. ej. Lin-AI)")
    reg = registro()
    if ida in reg:
        m = {**reg[ida]["manifiesto"], "grupo": grupo}
        extra = {"grupos_extra": _grupos_extra(d["grupos_extra"])} if "grupos_extra" in d else {}
        guardar_registro(m, reg[ida].get("repo", ""), reg[ida].get("origen", "panel"), **extra)
        _keycloak_de(ida)
        quienes = sorted({g for e in ENTORNOS for g in grupos_app(cfg(), registro(), registro()[ida], e)})
        log(f"{ida}: la ven " + ", ".join(f"/{g}" for g in quienes) + " (y sus subgrupos), e IT")
    elif RUTA.match(ida):
        f = DIR / "grupos"
        lineas = [l for l in (f.read_text().splitlines() if f.exists() else []) if l.split(None, 1)[:1] != [ida]]
        escribir(f, "\n".join(lineas + [f"{ida} {grupo}"]) + "\n")
        log(f"/{ida}/: la ven /DEV/{grupo} y /PRO/{grupo} (y sus subgrupos), e IT")
        try:
            kc_entrar()
            asegurar_grupos(grupo)
        except Error as e:
            log(f"aviso: Keycloak: {e}")
    else:
        raise Error("esa app no está dada de alta")
    aplicar_todo()


def _acc_principal(d: dict) -> None:
    """Crea (o renombra) una app principal: agrupa subapps bajo /<ruta>/."""
    m = validar_manifiesto({"ruta": d.get("ruta", ""), "nombre": d.get("nombre") or "", "grupo": d.get("grupo") or "",
                            "principal": True, "despliegue": {"tipo": "ninguno"}})
    if not m["grupo"]:
        m["grupo"] = m["nombre"] if GRUPO.match(m["nombre"]) else m["ruta"]
    reg = registro()
    if m["ruta"] in reg and not reg[m["ruta"]]["manifiesto"].get("principal"):
        raise Error(f"/{m['ruta']}/ ya es una app normal")
    guardar_registro(m, "", "panel")
    log(f"app principal /{m['ruta']}/: {m['nombre']} (sus subapps, en /{m['ruta']}/<subapp>/)")
    aplicar_todo()


def _acc_desplegar(d: dict) -> None:
    desplegar_app(_id_de(d), str(d.get("entorno", "")), str(d.get("version", "")).strip())


def _acc_dominio(d: dict) -> None:
    """PRO de una app (o de una principal, con sus subapps) en su dominio."""
    c = cfg()
    ida = _id_de(d)
    dom = str(d.get("dominio", "")).strip().lower().removeprefix("https://").split("/")[0]
    reg = registro()
    if ida not in reg or reg[ida]["manifiesto"].get("dentro"):
        raise Error("el dominio se pone a una app o a una app principal (no a una subapp)")
    if not DOMINIO.match(dom) or dom in (dominio(c, "dev"), dominio(c, "pro")):
        raise Error(f"dominio no válido: '{dom}'")
    otra = next((k for k, x in reg.items() if x.get("dominio") == dom and k != ida), "")
    if otra:
        raise Error(f"{dom} ya es de {otra}")
    if "pro" not in c["_gestionados"]:
        raise Error("la entrada de PRO aún no es del servidor")
    datos = reg[ida]
    m = datos["manifiesto"]
    guardar_registro(m, datos.get("repo", ""), datos.get("origen", "panel"), dominio=dom)
    sincronizar_dominio(ida, primera=True)
    log(f"LISTO: https://{dom}/ es {m['nombre']} de PRO." + (
        "" if m.get("principal") else f" pro…/{m['ruta']}/ sigue funcionando. La app tiene que aceptar esa dirección para el login (ver README)."))


def _acc_sincronizar(d: dict) -> None:
    sincronizar_dominio(_id_de(d))


def _acc_quitar_dominio(d: dict) -> None:
    """Marcha atras: el dominio vuelve a donde iba antes (si iba a algun sitio)."""
    ida = _id_de(d)
    reg = registro()
    if ida not in reg or not reg[ida].get("dominio"):
        raise Error("esa app no tiene dominio")
    datos = reg[ida]
    dom, antes = datos["dominio"], datos.get("npm_antes") or {}
    if antes.get("host"):
        npm_apuntar(dom, antes["host"], int(antes.get("puerto") or 80))
        log(f"{dom} vuelve a {antes['host']}:{antes.get('puerto') or 80} en Nginx Proxy Manager")
    else:
        log(f"{dom} sigue en Nginx Proxy Manager (no iba a ningún sitio antes); el DNS no se toca")
    guardar_registro(datos["manifiesto"], datos.get("repo", ""), datos.get("origen", "panel"), dominio="", npm_antes={})
    (DIR / "estado" / "dominios" / f"{ida}.json").unlink(missing_ok=True)
    aplicar_todo()
    _reiniciar_panel(cfg())
    log(f"LISTO: {datos['manifiesto']['nombre']} de PRO vuelve a pro…{camino(registro(), registro()[ida], 'pro') or '/'}")


def ip_servidor(c: dict) -> str:
    """La IP publica de este servidor: SERVIDOR_IP o la de su dominio de PRO."""
    ip = c.get("SERVIDOR_IP", "")
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        return ip
    try:
        return socket.gethostbyname(dominio(c, "pro"))
    except OSError:
        return ""


def sincronizar_dominio(ida: str, primera: bool = False) -> dict:
    """Deja bien todo lo de su dominio (y arregla lo que falte): DNS, la entrada
    de PRO, Nginx Proxy Manager con su certificado y Keycloak. Idempotente."""
    c = cfg()
    reg = registro()
    datos = reg[ida]
    m = datos["manifiesto"]
    dom = datos["dominio"]
    principal = bool(m.get("principal"))
    log(f"1/4 DNS de {dom}")
    if (DIR / ".cloudflare.env").exists():
        cloudflare_registro(dom, ip_servidor(c))
    try:
        socket.gethostbyname(dom)
    except OSError:
        raise Error(f"{dom} aún no apunta a ningún sitio: " + (
            "Cloudflare tarda unos minutos; dale a «Sincronizar» luego" if (DIR / ".cloudflare.env").exists() else
            "crea en tu DNS el registro hacia este servidor (sin proxy, nube gris) o guarda en Ajustes el token de "
            "Cloudflare para que lo haga el servidor")) from None
    log(f"2/4 entrada de PRO: {dom} → {m['nombre']}")
    aplicar_entrada("pro")
    log("3/4 Nginx Proxy Manager, con certificado")
    antes = npm_apuntar(dom, "entrada-pro", 80)
    if antes and antes.get("host") not in ("entrada-pro", "") and not datos.get("npm_antes"):
        # iba a otro sitio (p. ej. un portal de antes): se apunta para poder volver
        if principal and CONTENEDOR.match(antes["host"]):
            copiar_estaticos(m["ruta"], antes["host"])
            aplicar_entrada("pro")
        guardar_registro(m, datos.get("repo", ""), datos.get("origen", "panel"), npm_antes=antes)
        log(f"{dom} iba a {antes['host']}:{antes['puerto']} (no se toca; «Quitar su dominio» vuelve ahí)")
    log("4/4 Keycloak")
    try:
        kc_entrar()
        realm = _realm(c)
        if principal:
            cliente = f"{c['KEYCLOAK_CLIENTE']}-{m['ruta']}"
            secreto = asegurar_cliente(cliente, f"Portada {m['nombre']}", [f"https://{dom}/ajustes"],
                                       datos.get("tema") or "keycloak")
            f = DIR / "secretos" / f"{cliente}.env"
            if leer_env(f).get("KEYCLOAK_CLIENT_SECRET") != secreto:
                f.parent.mkdir(mode=0o700, exist_ok=True)
                poner_env(f, {"KEYCLOAK_CLIENT_ID": cliente, "KEYCLOAK_CLIENT_SECRET": secreto})
            lista = list(subapps(reg, m["ruta"]).values())
        else:
            lista = [datos]
        for x in lista:
            cli = nombre_de(x["manifiesto"]["keycloak"].get("cliente", ""), x["manifiesto"]["ruta"], "pro")
            cid = _cliente_id(realm, cli) if cli else ""
            if cid:
                anadir_url_cliente(realm, cid, f"https://{dom}" + (f"/{x['manifiesto']['ruta']}" if principal else ""))
    except Error as e:
        log(f"aviso: Keycloak: {e}")
    if principal:
        _reiniciar_panel(cfg())   # el login de su portada
    estado = comprobar_dominio(ida)
    log("estado: " + ", ".join(f"{k} {'✓' if v.get('ok') else '✗ ' + v.get('detalle', '')}" for k, v in estado["partes"].items()))
    return estado


def comprobar_dominio(ida: str) -> dict:
    """Mira (sin cambiar nada) si su dominio esta bien y lo deja en estado/dominios/<id>.json."""
    c = cfg()
    datos = registro().get(ida) or {}
    dom = datos.get("dominio", "")
    partes = {}
    ip = ip_servidor(c)
    try:
        vista = socket.gethostbyname(dom)
        partes["dns"] = {"ok": not ip or vista == ip, "detalle": "" if not ip or vista == ip else f"apunta a {vista}, no a {ip}"}
    except OSError:
        partes["dns"] = {"ok": False, "detalle": "no apunta a ningún sitio"}
    try:
        h = npm_host(dom)
        partes["proxy"] = {"ok": bool(h) and h.get("forward_host") == "entrada-pro",
                           "detalle": "" if h and h.get("forward_host") == "entrada-pro" else
                           (f"va a {h.get('forward_host')}" if h else "no está en Nginx Proxy Manager")}
        partes["certificado"] = {"ok": bool(h) and bool(h.get("certificate_id")), "detalle": "" if h and h.get("certificate_id") else "sin certificado"}
    except Error as e:
        partes["proxy"] = {"ok": False, "detalle": str(e)[:120]}
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://{dom}/ajustes/salud", method="GET"), timeout=10) as r:
            partes["responde"] = {"ok": r.status == 200, "detalle": ""}
    except Exception as e:   # noqa: BLE001 - cualquier fallo es "no responde"
        partes["responde"] = {"ok": False, "detalle": str(e)[:120]}
    estado = {"dominio": dom, "fecha": ahora(), "ok": all(p["ok"] for p in partes.values()), "partes": partes}
    escribir(DIR / "estado" / "dominios" / f"{ida}.json", json.dumps(estado, ensure_ascii=False) + "\n")
    return estado


def leer_estado_dominio(ida: str) -> dict:
    try:
        return json.loads((DIR / "estado" / "dominios" / f"{ida}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def contenedor_de(alias: str) -> str:
    """El contenedor que hay detrás de un nombre de NPM: el suyo, o uno con ese alias de red
    (p. ej. spsil-portal es el alias de spsil-plataforma-portal-1)."""
    try:
        nombres = run(["docker", "ps", "--format", "{{.Names}}"], timeout=30).split()
        if alias in nombres:
            return alias
        for n in nombres:
            redes = json.loads(run(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", n], timeout=30) or "{}")
            if any(alias in (r.get("Aliases") or []) for r in redes.values()):
                return n
    except (Error, ValueError, subprocess.TimeoutExpired):
        pass
    return ""


def copiar_estaticos(ruta: str, contenedor: str) -> None:
    """El logo, /marca/ y /js/ del portal que servia antes el dominio (solo si aun no hay nada)."""
    import tempfile
    destino = DIR / "estado" / "estaticos" / ruta
    if destino.is_dir() and any(destino.iterdir()):
        return
    with tempfile.TemporaryDirectory() as tmp:
        nombre = contenedor_de(contenedor)
        if not nombre or subprocess.run(["docker", "cp", f"{nombre}:/usr/share/nginx/html/.", tmp],
                                        capture_output=True).returncode != 0:
            log(f"no pude copiar el logo de {contenedor}: súbelo a mano a estado/estaticos/{ruta}/")
            return
        origen = Path(tmp)
        destino.mkdir(parents=True, exist_ok=True)
        for f in origen.iterdir():
            if f.is_file() and not f.is_symlink() and ESTATICO.match(f.name):
                shutil.copyfile(f, destino / f.name)
        for carpeta in CARPETAS_FIJAS:
            base = origen / carpeta
            if not base.is_dir() or base.is_symlink():
                continue
            for f in base.rglob("*"):
                rel = f.relative_to(base)
                if f.is_file() and not f.is_symlink() and all(re.fullmatch(r"[A-Za-z0-9._-]+", x) for x in rel.parts):
                    (destino / carpeta / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, destino / carpeta / rel)
    log(f"logo y ficheros fijos de {contenedor} copiados para la portada de /{ruta}/")


def _acc_principal_ajustes(d: dict) -> None:
    """Tema del login, logo y rutas viejas de la portada de una principal en su dominio."""
    ida = _id_de(d)
    reg = registro()
    if not reg.get(ida, {}).get("manifiesto", {}).get("principal"):
        raise Error("no es una app principal")
    tema = str(d.get("tema", "")).strip()
    logo = str(d.get("logo", "")).strip().lstrip("/")
    if tema and not CONTENEDOR.match(tema):
        raise Error("tema no válido")
    if logo and not ESTATICO.match(logo):
        raise Error("logo no válido (p. ej. logo.webp)")
    red = {}
    for par in str(d.get("redirigir", "")).replace(",", " ").split():
        r, _, dest = par.partition(">")
        if not (RUTA.match(r) and r not in ("ajustes", "portada") and DESTINO.match(dest)):
            raise Error(f"redirección no válida: '{par}' (ruta>/destino/ o ruta>https://...)")
        red[r] = dest
    datos = reg[ida]
    guardar_registro(datos["manifiesto"], datos.get("repo", ""), datos.get("origen", "panel"), tema=tema, logo=logo, redirigir=red)
    log(f"{ida}: guardado (tema {tema or 'keycloak'}, logo {logo or 'ninguno'}, {len(red)} rutas viejas)")
    aplicar_todo()
    if datos.get("dominio"):
        sincronizar_dominio(ida)


def _acc_npm(d: dict) -> None:
    email, clave = str(d.get("email", "")).strip(), str(d.get("clave", ""))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or not clave or "\n" in clave:
        raise Error("email o contraseña no válidos")
    anterior = (DIR / ".npm.env").read_text() if (DIR / ".npm.env").exists() else None
    poner_env(DIR / ".npm.env", {"NPM_EMAIL": email, "NPM_CLAVE": clave})
    try:
        npm_token()
    except Error:
        if anterior is None:
            (DIR / ".npm.env").unlink()
        else:
            escribir(DIR / ".npm.env", anterior, modo=0o600)
        raise
    log("acceso a Nginx Proxy Manager guardado")


ACCIONES = {"registrar": _acc_registrar, "quitar": _acc_quitar, "grupo": _acc_grupo,
            "desplegar": _acc_desplegar, "dominio": _acc_dominio, "npm": _acc_npm,
            "principal": _acc_principal, "principal_ajustes": _acc_principal_ajustes,
            "sincronizar": _acc_sincronizar, "quitar_dominio": _acc_quitar_dominio, "cloudflare": _acc_cloudflare}


def procesar_peticiones() -> int:
    global _SALIDA
    entrada, salida = DIR / "peticiones", DIR / "estado" / "resultados"
    hechas = 0
    for f in sorted(entrada.glob("*.json")):
        pid = f.stem
        try:
            pet = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pet = {}
        f.unlink(missing_ok=True)
        if not ID_PETICION.match(pid) or not isinstance(pet, dict):
            continue
        accion = str(pet.get("accion", ""))
        _SALIDA = []
        res = {"id": pid, "accion": accion, "quien": str(pet.get("quien", ""))[:80],
               "pedida": str(pet.get("fecha", ""))[:30], "inicio": ahora()}
        try:
            if accion not in ACCIONES:
                raise Error(f"acción desconocida: '{accion}'")
            ACCIONES[accion](pet.get("datos") or {})
            res["ok"] = True
        except Error as e:
            log(f"ERROR: {e}")
            res["ok"] = False
        except Exception as e:   # que una peticion rara no tumbe al agente
            log(f"ERROR inesperado: {type(e).__name__}: {e}")
            res["ok"] = False
        res["fin"], res["salida"] = ahora(), "\n".join(_SALIDA[-60:])
        _SALIDA = None
        if accion == "npm":
            res["salida"] = "\n".join(l for l in res["salida"].splitlines() if "@" not in l)
        if accion == "cloudflare":
            tok = str((pet.get("datos") or {}).get("token", ""))
            if tok:
                res["salida"] = res["salida"].replace(tok, "•••")
        escribir(salida / f"{pid}.json", json.dumps(res, ensure_ascii=False, indent=1) + "\n")
        hechas += 1
    viejos = sorted(salida.glob("*.json"))[:-40] if salida.exists() else []
    for v in viejos:
        v.unlink(missing_ok=True)
    return hechas


def aplicar_todo() -> None:
    for ent in cfg()["_gestionados"]:
        try:
            aplicar_entrada(ent)
        except Error as e:
            log(str(e))
    escribir_catalogo()


# ── el panel (portada y Ajustes) ─────────────────────────────────────────────

def escribir_panel(c: dict, version: str) -> Path:
    d = DIR / "panel"
    claves = ("DOMINIO_DEV", "DOMINIO_PRO", "KEYCLOAK_ISSUER", "KEYCLOAK_INTERNO", "KEYCLOAK_CLIENTE",
              "KEYCLOAK_SECRETO", "GRUPO_IT", "PANEL_CLAVE", "AJUSTES_EN")
    valores = {k: c.get(k, "") for k in claves}
    valores["AJUSTES_EN"] = c["AJUSTES_EN"]
    # el login de la portada de cada app principal en su dominio (su cliente)
    for ida, datos in registro().items():
        if datos["manifiesto"].get("principal") and datos.get("dominio"):
            cli = f"{c['KEYCLOAK_CLIENTE']}-{ida}"
            clave = ida.upper().replace("-", "_")
            valores[f"KEYCLOAK_CLIENTE_{clave}"] = cli
            valores[f"KEYCLOAK_SECRETO_{clave}"] = leer_env(DIR / "secretos" / f"{cli}.env").get("KEYCLOAK_CLIENT_SECRET", "")
    escribir(d / "panel.env", "".join(f"{k}={v}\n" for k, v in valores.items()), modo=0o600)
    escribir(d / ".env", f"PANEL_VERSION={version}\n")
    escribir(d / "docker-compose.yml", f"""# Portada y Ajustes (lo escribe servidor.py)
name: servidor-panel
services:
  panel:
    image: servidor-panel:${{PANEL_VERSION:?}}
    pull_policy: never
    container_name: {PANEL}
    restart: unless-stopped
    mem_limit: 192m
    env_file: [panel.env]
    volumes:
      - datos:/datos
      - ../estado:/estado:ro
      - ../peticiones:/peticiones
    networks:
      proxy:
        aliases: [{PANEL}]
volumes:
  datos:
networks:
  proxy:
    name: {c['RED_PROXY']}
    external: true
""")
    return d


def _salud_panel() -> str:
    r = subprocess.run(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                        PANEL], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "parado"


def desplegar_panel(version: str) -> None:
    """Construye la imagen del panel con el codigo actual y la pone; si no
    arranca, vuelve a la de antes."""
    c = cfg()
    d = DIR / "panel"
    anterior = leer_env(d / ".env").get("PANEL_VERSION", "")
    log(f"panel: construyendo {version}")
    run(["docker", "build", "-q", "--build-arg", f"VERSION={version}", "-t", f"servidor-panel:{version}",
         str(DIR / "codigo" / "panel")], timeout=900)
    escribir_panel(c, version)
    compose = ["docker", "compose", "-f", str(d / "docker-compose.yml"), "--project-directory", str(d)]
    run([*compose, "up", "-d", "--remove-orphans"], timeout=300)
    t = 0
    while t < 120:
        estado = _salud_panel()
        if estado == "healthy":
            log(f"panel en {version}")
            for img in run(["docker", "images", "servidor-panel", "--format", "{{.Tag}}"]).split():
                if img not in (version, anterior):
                    subprocess.run(["docker", "rmi", f"servidor-panel:{img}"], capture_output=True)
            return
        time.sleep(3)
        t += 3
    if anterior and anterior != version:
        escribir_panel(c, anterior)
        subprocess.run([*compose, "up", "-d"], capture_output=True)
    raise Error(f"el panel {version} no arranca (docker logs {PANEL}); sigue {anterior or 'nada'}")


# ── actualizarse solo (git del repo, solo commits con las pruebas en verde) ──

def _malas() -> set:
    f = DIR / "estado" / "versiones-malas"
    return set(f.read_text().split()) if f.exists() else set()


def _marcar_mala(sha: str, motivo: str) -> None:
    f = DIR / "estado" / "versiones-malas"
    with f.open("a") as h:
        h.write(sha + "\n")
    with (DIR / "historial.log").open("a") as h:
        h.write(f"{ahora()}\t{sha[:7]}\tdescartada\t{motivo}\n")
    log(f"versión {sha[:7]} descartada: {motivo}")


def pruebas_de(repo: str, sha: str) -> str:
    """ok | esperando | mal, segun las comprobaciones de GitHub de ese commit."""
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "servidor"}), timeout=15) as r:
            datos = json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return "esperando"
    runs = datos.get("check_runs", [])
    if not runs or any(x.get("status") != "completed" for x in runs):
        return "esperando"
    return "ok" if all(x.get("conclusion") in ("success", "skipped", "neutral") for x in runs) else "mal"


def actualizar(forzar_panel: bool = False) -> bool:
    """True si ha cambiado el codigo (el agente se reinicia para usarlo)."""
    c = cfg()
    git = ["git", "-C", str(DIR / "codigo")]
    try:
        run([*git, "fetch", "-q", "--depth", "50", "origin", c["RAMA"]], timeout=120)
    except Error as e:
        log(f"aviso: no puedo mirar si hay versión nueva: {e}")
        return False
    nuevo = run([*git, "rev-parse", "FETCH_HEAD"]).strip()
    actual = run([*git, "rev-parse", "HEAD"]).strip()
    if nuevo == actual:
        if forzar_panel:
            desplegar_panel(actual[:7])
        return False
    if nuevo in _malas():
        return False
    estado = pruebas_de(c["REPO"], nuevo)
    if estado == "esperando":
        return False
    if estado == "mal":
        _marcar_mala(nuevo, "las pruebas de GitHub fallan")
        return False
    run([*git, "checkout", "-q", "--detach", nuevo])
    r = subprocess.run([sys.executable, str(DIR / "codigo" / "servidor.py"), "comprobar"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        run([*git, "checkout", "-q", "--detach", actual])
        _marcar_mala(nuevo, "no pasa 'servidor comprobar': " + (r.stderr or r.stdout).strip()[-300:])
        return False
    try:
        desplegar_panel(nuevo[:7])
    except Error as e:
        run([*git, "checkout", "-q", "--detach", actual])
        _marcar_mala(nuevo, str(e))
        return False
    with (DIR / "historial.log").open("a") as h:
        h.write(f"{ahora()}\t{nuevo[:7]}\tok\tde {actual[:7]}\n")
    log(f"servidor actualizado: {actual[:7]} -> {nuevo[:7]}")
    return True


# ── el agente (servicio de systemd) ──────────────────────────────────────────

def agente() -> None:
    cerrojo = open(DIR / ".agente.lock", "w")
    try:
        fcntl.flock(cerrojo, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("ya hay otro agente en marcha")
    preparar_carpetas()
    log(f"agente en marcha (versión {version_actual()})")
    aplicar_todo()
    ultima = 0.0
    ultima_dominios = 0.0
    while True:
        try:
            procesar_peticiones()
            aplicar_todo()
            if time.time() - ultima_dominios > CADA_DOMINIOS:
                ultima_dominios = time.time()
                for ida, datos in registro().items():
                    if datos.get("dominio"):
                        comprobar_dominio(ida)
            if time.time() - ultima > CADA_ACTUALIZAR:
                ultima = time.time()
                if actualizar():
                    log("reinicio para usar el código nuevo")
                    sys.exit(0)   # systemd lo vuelve a arrancar (Restart=always)
        except Error as e:
            log(f"aviso: {e}")
        except Exception as e:   # sigue vivo pase lo que pase
            log(f"aviso inesperado: {type(e).__name__}: {e}")
        time.sleep(3)


def preparar_carpetas() -> None:
    for sub, modo in (("registro", 0o755), ("estado", 0o755), ("estado/resultados", 0o755),
                      ("estado/dominios", 0o755), ("estado/estaticos", 0o755),
                      ("peticiones", 0o700), ("secretos", 0o700), ("entrada", 0o755)):
        (DIR / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(DIR / sub, modo)
    try:
        os.chown(DIR / "peticiones", PANEL_UID, PANEL_UID)   # el panel escribe ahi
    except PermissionError:
        pass


# ── instalar, tomar y soltar una entrada ─────────────────────────────────────

SERVICIO = """[Unit]
Description=Servidor DEV/PRO: entrada, portada, Ajustes y apps
After=docker.service
Requires=docker.service

[Service]
Environment=SERVIDOR_DIR={dir}
ExecStart=/usr/bin/python3 {dir}/codigo/servidor.py agente
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _url_tareas(ent: str) -> str:
    """Dominio de un entorno sacado de la instalacion de Tareas de antes (solo al instalar)."""
    for clave in ("APP_URLS_ALTERNATIVAS", "APP_URL_PUBLICA"):
        for u in leer_env(Path(f"/opt/apps/tareas/{ent}/app.env")).get(clave, "").split(","):
            m = re.fullmatch(r"https://([^/]+)/tareas/?", u.strip())
            if m:
                return m.group(1)
    u = leer_env(Path(f"/opt/apps/tareas/{ent}/.env")).get("PORTADA_URL", "")
    return u.removeprefix("https://").strip("/")


def _preguntar(texto: str, defecto: str = "") -> str:
    if not sys.stdin.isatty():
        raise Error(f"no lo sé sacar de lo que hay en el servidor: {texto}. "
                    f"Escríbelo en {DIR / 'servidor.env'} y vuelve a ejecutar 'servidor instalar'")
    r = input(f"{texto}{f' [{defecto}]' if defecto else ''}: ").strip()
    return r or defecto


def configurar() -> dict:
    """servidor.env la primera vez: lo que se pueda, de la instalacion de antes."""
    f = DIR / "servidor.env"
    c = leer_env(f)
    nuevos = {}
    antes_dev = leer_env(Path("/opt/apps/tareas/dev/app.env"))
    for ent in ENTORNOS:
        clave = f"DOMINIO_{ent.upper()}"
        if not c.get(clave):
            nuevos[clave] = _url_tareas(ent) or _preguntar(f"Dominio de {ent.upper()} (p. ej. {ent}.midominio.com)")
    if not c.get("KEYCLOAK_ISSUER"):
        nuevos["KEYCLOAK_ISSUER"] = antes_dev.get("KEYCLOAK_ISSUER") or _preguntar(
            "Issuer de Keycloak (https://.../realms/REALM)")
    if not c.get("RED_PROXY"):
        nuevos["RED_PROXY"] = leer_env(Path("/opt/apps/tareas/dev/.env")).get("RED_PROXY") or "proxy_default"
    if not c.get("GRUPO_IT"):
        nuevos["GRUPO_IT"] = antes_dev.get("KEYCLOAK_GRUPO_ADMIN_GLOBAL") or "IT"
    if "LEGADO" not in c and Path("/opt/apps/tareas/entrada").is_dir():
        nuevos["LEGADO"] = "/opt/apps/tareas/entrada"
    if not c.get("PANEL_CLAVE"):
        nuevos["PANEL_CLAVE"] = secrets.token_hex(32)
    if not c.get("REPO"):
        nuevos["REPO"] = REPO_POR_DEFECTO
    if "ENTORNOS_GESTIONADOS" not in c:
        nuevos["ENTORNOS_GESTIONADOS"] = ""
    for k, v in nuevos.items():
        if k.startswith("DOMINIO_") and not DOMINIO.match(v):
            raise Error(f"{k} no válido: '{v}'")
    if nuevos:
        poner_env(f, nuevos)
    return cfg()


def cliente_panel(c: dict) -> None:
    kc_entrar()
    urls = [f"https://{dominio(c, e)}/ajustes" for e in ENTORNOS if dominio(c, e)]
    secreto = asegurar_cliente(c["KEYCLOAK_CLIENTE"], "Portada y Ajustes", urls)
    if secreto and secreto != c.get("KEYCLOAK_SECRETO"):
        poner_env(DIR / "servidor.env", {"KEYCLOAK_SECRETO": secreto})


def tomar(ent: str) -> None:
    """La entrada de ENT pasa a ser del servidor (portada y Ajustes del panel,
    rutas de las apps; las que aun escriben su .conf siguen igual)."""
    if ent not in ENTORNOS:
        raise Error("entorno: dev o pro")
    c = cfg()
    gest = c["_gestionados"] + ([ent] if ent not in c["_gestionados"] else [])
    poner_env(DIR / "servidor.env", {"ENTORNOS_GESTIONADOS": " ".join(gest),
                                     "AJUSTES_EN": "pro" if "pro" in gest else gest[0]})
    c = cfg()
    base = DIR / "entrada" / ent
    _volcar(base, ficheros_entrada(ent))
    nginx_valida(base)
    p = compose_entrada(c)
    subprocess.run(["docker", "rm", "-f", f"entrada-{ent}"], capture_output=True)
    run(["docker", "compose", "-f", str(p), "--project-directory", str(p.parent), "up", "-d", ent], timeout=300)
    log(f"{ent}: la entrada ya es del servidor")
    escribir_catalogo()
    _reiniciar_panel(c)   # el panel sabe donde va Ajustes


def _reiniciar_panel(c: dict) -> None:
    """Que el panel sepa lo nuevo (donde va Ajustes, el login de cada dominio)."""
    v = leer_env(DIR / "panel" / ".env").get("PANEL_VERSION", "")
    if v:
        d = DIR / "panel"
        antes = (d / "panel.env").read_text() if (d / "panel.env").exists() else ""
        escribir_panel(c, v)
        if (d / "panel.env").read_text() != antes:
            run(["docker", "compose", "-f", str(d / "docker-compose.yml"), "--project-directory", str(d), "up", "-d"])


def soltar(ent: str) -> None:
    """Marcha atras: la entrada de ENT vuelve a la de antes (la de Tareas)."""
    c = cfg()
    base = legado(c)
    if not base or not (base / "docker-compose.yml").exists():
        raise Error("no hay entrada de antes a la que volver")
    gest = [e for e in c["_gestionados"] if e != ent]
    poner_env(DIR / "servidor.env", {"ENTORNOS_GESTIONADOS": " ".join(gest),
                                     "AJUSTES_EN": "pro" if "pro" in gest else (gest[0] if gest else "pro")})
    compose_entrada(cfg())
    subprocess.run(["docker", "rm", "-f", f"entrada-{ent}"], capture_output=True)
    run(["docker", "compose", "-f", str(base / "docker-compose.yml"), "--project-directory", str(base),
         "up", "-d", ent], timeout=300)
    log(f"{ent}: vuelve a la entrada de antes ({base})")
    escribir_catalogo()


def importar_de_tareas() -> None:
    """Una vez, al instalar: lo que tenia el panel de Ajustes de Tareas (el
    token de GitHub y las apps) pasa al panel nuevo. Si algo no esta, se sigue."""
    tareas = Path("/opt/apps/tareas")
    pro = leer_env(tareas / "pro" / ".env")
    root = leer_env(tareas / "bd" / ".env").get("MARIADB_ROOT_PASSWORD", "")
    bd = pro.get("DB_NAME", "")
    if not (root and re.fullmatch(r"\w+", bd or "")):
        return
    r = subprocess.run(["docker", "exec", "-e", f"MYSQL_PWD={root}", "tareas-bd", "mariadb", "-uroot", "-N", "-B", "-e",
                        f"SELECT clave, valor FROM `{bd}`.ajustes_app WHERE clave LIKE 'despliegue.%'"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log("aviso: no pude leer los Ajustes de antes (se pueden rellenar en el panel)")
        return
    a = dict(l.split("\t", 1) for l in r.stdout.splitlines() if "\t" in l)
    sueltos = grupos_sueltos()
    grupo_tareas = leer_env(tareas / "dev" / "app.env").get("KEYCLOAK_GRUPO_APP", "").strip("/")
    for pre in ("DEV/", "PRO/"):
        grupo_tareas = grupo_tareas.removeprefix(pre)
    reg = registro()
    if a.get("despliegue.repo") and "tareas" not in reg:
        try:
            m = validar_manifiesto({"ruta": "tareas", "nombre": leer_env(tareas / "pro" / "app.env").get("APP_NOMBRE") or "Tareas",
                                    "grupo": grupo_tareas if GRUPO.match(grupo_tareas or "-") else "",
                                    "entrada": "propia", "prefijo": "conservar", "contenedor": "tareas-{entorno}",
                                    "despliegue": {"tipo": "workflow", "workflow": a.get("despliegue.workflow") or "despliegue.yml",
                                                   "rama": a.get("despliegue.rama") or "main", "anterior": True}},
                                   a["despliegue.repo"])
            guardar_registro(m, a["despliegue.repo"], origen="importada")
            log("Tareas dada de alta con lo de antes")
        except Error as e:
            log(f"aviso: Tareas: {e}")
    try:
        externas = json.loads(a.get("despliegue.externas") or "[]")
    except ValueError:
        externas = []
    for x in externas if isinstance(externas, list) else []:
        try:
            ruta = str(x.get("ruta", ""))
            if ruta in registro():
                continue
            m = validar_manifiesto({"ruta": ruta, "nombre": x.get("nombre"), "grupo": sueltos.get(ruta, ""),
                                    "entrada": "propia", "version": x.get("salud") or "version/",
                                    "despliegue": {"tipo": "workflow", "workflow": x.get("workflow"), "rama": x.get("rama"),
                                                   "campo_entorno": x.get("campo_entorno"), "campo_version": x.get("campo_version"),
                                                   "anterior": bool(x.get("anterior"))}}, str(x.get("repo", "")))
            guardar_registro(m, str(x.get("repo", "")), origen="importada")
            log(f"/{ruta}/ dada de alta con lo de antes")
        except Error as e:
            log(f"aviso: {x.get('ruta')}: {e}")
    token = a.get("despliegue.token", "")
    if token.startswith("cifrado:") and pro.get("SESSION_SECRET_KEY"):
        datos = json.dumps({"cifrado": token[len("cifrado:"):], "clave": pro["SESSION_SECRET_KEY"]})
        r = subprocess.run(["docker", "exec", "-i", PANEL, "python", "-m", "panel.importar"],
                           input=datos, capture_output=True, text=True)
        log("token de GitHub traído de los Ajustes de antes" if r.returncode == 0
            else "aviso: no pude traer el token de GitHub (se pega una vez en Ajustes)")


def instalar(entornos: list[str]) -> None:
    if os.geteuid() != 0:
        raise Error("hay que ejecutarlo como root")
    for prog in ("docker", "git"):
        if not shutil.which(prog):
            raise Error(f"falta {prog}")
    DIR.mkdir(parents=True, exist_ok=True)
    preparar_carpetas()
    c = configurar()
    if subprocess.run(["docker", "network", "inspect", c["RED_PROXY"]], capture_output=True).returncode != 0:
        raise Error(f"no existe la red Docker '{c['RED_PROXY']}' del proxy")
    log("1/5 Keycloak: cliente de la portada y Ajustes")
    cliente_panel(c)
    c = cfg()
    log("2/5 portada y Ajustes (panel)")
    desplegar_panel(version_actual() or "local")
    importar_de_tareas()
    log("3/5 entradas")
    for ent in entornos:
        tomar(ent)
    log("4/5 servicio que se actualiza solo")
    escribir(Path("/etc/systemd/system/servidor-agente.service"), SERVICIO.format(dir=DIR))
    enlace = Path("/usr/local/bin/servidor")
    if not enlace.exists():
        enlace.symlink_to(DIR / "codigo" / "servidor.py")
    # que un cambio de permisos no cuente como cambio local (si no, git no actualiza)
    run(["git", "-C", str(DIR / "codigo"), "config", "core.fileMode", "false"])
    if Path("/run/systemd/system").exists():
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", "servidor-agente"])
        run(["systemctl", "restart", "servidor-agente"])
    else:
        log("aviso: este sistema no usa systemd: arranca el agente a mano (servidor agente)")
    log("5/5 comprobación")
    time.sleep(5)
    estado()
    c = cfg()
    print(f"\nLISTO. Portada: https://{dominio(c, entornos[0] if entornos else 'pro')}/   "
          f"Ajustes: https://{dominio(c, c['AJUSTES_EN'])}/ajustes/")
    print("Marcha atrás si algo no va: servidor soltar " + " ".join(entornos or ["ENT"]))


def estado() -> None:
    c = cfg()
    print(f"servidor {version_actual() or '?'} · gestiona: {' '.join(c['_gestionados']) or 'ninguna entrada'} · "
          f"Ajustes en {c['AJUSTES_EN'].upper()}")
    print(f"panel: {_salud_panel()} ({leer_env(DIR / 'panel' / '.env').get('PANEL_VERSION', '-')})")
    agente_vivo = subprocess.run(["systemctl", "is-active", "servidor-agente"], capture_output=True, text=True).stdout.strip()
    print(f"agente: {agente_vivo or '?'}")
    for ent in ENTORNOS:
        apps = catalogo()["entradas"][ent]
        print(f"{ent.upper()} https://{dominio(c, ent)}/ : " + (", ".join(f"/{a['ruta']}/" for a in apps) or "sin apps"))


def comprobar() -> None:
    """Lo que el agente prueba antes de quedarse con una version nueva: que
    el codigo carga y genera una configuracion que nginx acepta, sin tocar nada."""
    import tempfile
    for ent in cfg()["_gestionados"] or ["dev"]:
        ficheros = ficheros_entrada(ent)
        with tempfile.TemporaryDirectory() as tmp:
            _volcar(Path(tmp), ficheros)
            os.chmod(tmp, 0o755)
            if shutil.which("docker"):
                nginx_valida(Path(tmp))
    catalogo()
    print("ok")


def _bootstrap(args: list[str]) -> None:
    """Primera vez, ejecutado desde curl | python3 -: baja el repositorio y
    sigue con la copia de verdad."""
    repo = os.environ.get("SERVIDOR_REPO", REPO_POR_DEFECTO)
    codigo = DIR / "codigo"
    if not (codigo / ".git").exists():
        DIR.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "-q", f"https://github.com/{repo}.git", str(codigo)], timeout=300)
    os.execv(sys.executable, [sys.executable, str(codigo / "servidor.py"), *args])


def main(argv: list[str]) -> None:
    orden = argv[1] if len(argv) > 1 else ""
    resto = argv[2:]
    try:
        if orden == "instalar" and not Path(__file__).resolve().is_relative_to(DIR / "codigo") \
                and not os.environ.get("SERVIDOR_SIN_BOOTSTRAP"):
            _bootstrap(argv[1:])
        if orden == "instalar":
            entornos = [e for e in resto if e in ENTORNOS]
            instalar(entornos or ["dev"])
        elif orden == "tomar" and resto:
            tomar(resto[0])
        elif orden == "soltar" and resto:
            soltar(resto[0])
        elif orden == "entrada":
            for ent in cfg()["_gestionados"]:
                aplicar_entrada(ent, forzar_recarga=True)
            escribir_catalogo()
        elif orden == "agente":
            agente()
        elif orden == "actualizar":
            print("actualizado" if actualizar(forzar_panel="--panel" in resto) else "sin cambios")
        elif orden == "comprobar":
            comprobar()
        elif orden == "estado":
            estado()
        elif orden == "peticiones":
            print(procesar_peticiones())
        else:
            print(__doc__)
            sys.exit(1)
    except Error as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if "__file__" not in globals():   # curl ... | python3 - instalar
        __file__ = "/dev/stdin"
    main(sys.argv)
