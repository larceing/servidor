# -*- coding: utf-8 -*-
"""Pruebas del panel (portada y Ajustes). Keycloak y GitHub, simulados.
  cd panel && python -m unittest discover -s tests -v"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ.update({
    "PANEL_CLAVE": "clave-de-pruebas", "SESION_SEGURA": "0",
    "DOMINIO_DEV": "dev.ejemplo.com", "DOMINIO_PRO": "pro.ejemplo.com", "AJUSTES_EN": "pro",
    "KEYCLOAK_ISSUER": "https://usuarios.ejemplo.com/auth/realms/general", "KEYCLOAK_CLIENTE": "servidor",
    "KEYCLOAK_SECRETO": "x", "GRUPO_IT": "IT",
    "ESTADO_DIR": f"{TMP}/estado", "PETICIONES_DIR": f"{TMP}/peticiones", "DATOS_DIR": f"{TMP}/datos",
})
for d in ("estado/resultados", "peticiones", "datos"):
    Path(TMP, d).mkdir(parents=True, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from panel import app as panel, claves, en_curso, github, guardado, importar  # noqa: E402

CATALOGO = {
    "version": "abc1234", "latido": "2999-01-01T00:00:00Z", "dominios": {"dev": "dev.ejemplo.com", "pro": "pro.ejemplo.com"},
    "entradas": {
        "dev": [{"ruta": "tareas", "nombre": "Tareas", "salud": "version/", "grupo": "DEV/Tareas"},
                {"ruta": "linai", "nombre": "Lin-AI", "salud": "version/", "grupo": "DEV/Lin-AI"},
                {"ruta": "secreta", "nombre": "Secreta", "salud": "version/", "grupo": ""}],
        "pro": [{"ruta": "tareas", "nombre": "Tareas", "salud": "version/", "grupo": "PRO/Tareas"}]},
    "apps": [{"ruta": "tareas", "nombre": "Tareas", "grupo": "Tareas", "version": "version/", "repo": "yo/tareas",
              "despliegue": {"tipo": "workflow", "repo": "yo/tareas", "workflow": "despliegue.yml", "rama": "main",
                             "campo_entorno": "entorno", "campo_version": "version", "anterior": True}},
             {"ruta": "inventario", "nombre": "Inventario", "grupo": "Inventario", "version": "version/",
              "despliegue": {"tipo": "servidor", "imagen": "ghcr.io/yo/inventario"},
              "versiones": {"dev": "sha-1111111", "pro": ""}}],
    "sin_alta": [{"ruta": "linai", "grupo": "Lin-AI"}], "npm": False,
}


def poner_catalogo(c=CATALOGO):
    Path(TMP, "estado", "catalogo.json").write_text(json.dumps(c))


def cliente(usuario=None, grupos=(), entorno="pro"):
    c = TestClient(panel.app, base_url=f"http://{os.environ['DOMINIO_' + entorno.upper()]}")
    c.headers["X-Entorno"] = entorno
    if usuario:
        datos = {"usuario": usuario, "grupos": sorted(g.strip("/") for g in grupos), "id_token": "tok"}
        with mock.patch.object(panel, "canjear_codigo", return_value=datos):
            r = c.get("/ajustes/entrar?next=/", follow_redirects=False)
            from urllib.parse import parse_qs, urlparse
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            r = c.get(f"/ajustes/entrar/vuelta?code=abc&state={state}", follow_redirects=False)
            assert r.status_code == 303, r.text
    return c


def sin_versiones(*_a, **_k):
    return {"ok": True, "version": "sha-2222222", "detalle": ""}


class Portada(unittest.TestCase):
    def setUp(self):
        poner_catalogo()

    def test_sin_sesion_pide_login(self):
        r = cliente().get("/portada/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/ajustes/entrar"))

    def test_login_lleva_a_keycloak_con_vuelta_del_entorno(self):
        r = cliente(entorno="dev").get("/ajustes/entrar?next=/", follow_redirects=False)
        self.assertIn("usuarios.ejemplo.com/auth/realms/general/protocol/openid-connect/auth", r.headers["location"])
        self.assertIn("https%3A%2F%2Fdev.ejemplo.com%2Fajustes%2Fentrar%2Fvuelta", r.headers["location"])

    def test_vuelta_con_state_malo_no_entra(self):
        c = cliente()
        c.get("/ajustes/entrar?next=/", follow_redirects=False)
        with mock.patch.object(panel, "canjear_codigo", return_value={"usuario": "x", "grupos": ["IT"], "id_token": ""}):
            r = c.get("/ajustes/entrar/vuelta?code=a&state=otro", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/ajustes/entrar?next=/")
        self.assertEqual(c.get("/portada/", follow_redirects=False).status_code, 303)

    def test_cada_uno_ve_sus_apps(self):
        r = cliente("ana", ["/DEV/Lin-AI/Editor"], entorno="dev").get("/portada/")
        self.assertIn("Lin-AI", r.text)
        self.assertNotIn(">Tareas<", r.text)
        self.assertNotIn("Secreta", r.text)
        self.assertNotIn("Ajustes", r.text)

    def test_grupo_de_pro_no_da_dev(self):
        r = cliente("ana", ["/PRO/Lin-AI"], entorno="dev").get("/portada/")
        self.assertIn("No tienes acceso", r.text)

    def test_it_ve_todo_y_ajustes_de_pro(self):
        r = cliente("henry", ["/IT"], entorno="dev").get("/portada/")
        for n in ("Tareas", "Lin-AI", "Secreta"):
            self.assertIn(n, r.text)
        self.assertIn("https://pro.ejemplo.com/ajustes/", r.text)

    def test_icono_de_la_app_en_su_tarjeta(self):
        cat = json.loads(json.dumps(CATALOGO))
        cat["entradas"]["dev"][0]["icono"] = "img/logo.png"
        cat["entradas"]["dev"][1]["icono"] = "../fuera.png"   # no vale: se queda la inicial
        poner_catalogo(cat)
        r = cliente("henry", ["/IT"], entorno="dev").get("/portada/")
        self.assertIn('src="/tareas/img/logo.png"', r.text)
        self.assertNotIn("fuera.png", r.text)

    def test_salir_cierra_keycloak_y_vuelve_a_la_portada(self):
        c = cliente("henry", ["/IT"])
        r = c.get("/ajustes/salir", follow_redirects=False)
        self.assertIn("protocol/openid-connect/logout", r.headers["location"])
        self.assertIn("post_logout_redirect_uri=https%3A%2F%2Fpro.ejemplo.com%2F", r.headers["location"])
        self.assertIn("id_token_hint=tok", r.headers["location"])
        self.assertEqual(c.get("/portada/", follow_redirects=False).status_code, 303)

    def test_next_no_puede_ir_fuera(self):
        self.assertEqual(panel._siguiente_valido("//malo.com"), "/")
        self.assertEqual(panel._siguiente_valido("https://malo.com"), "/")
        self.assertEqual(panel._siguiente_valido("/ajustes/"), "/ajustes/")

    def test_salud(self):
        self.assertEqual(cliente().get("/ajustes/salud").json()["ok"], True)
        self.assertEqual(cliente().head("/ajustes/salud").status_code, 200)


@mock.patch.object(panel, "version_en", sin_versiones)
class Ajustes(unittest.TestCase):
    def setUp(self):
        poner_catalogo()
        for p in Path(TMP, "peticiones").glob("*"):
            p.unlink()
        Path(TMP, "datos", "en_curso.json").unlink(missing_ok=True)

    def csrf(self, c):
        import re
        return re.search(r'name="csrf" value="([^"]+)"', c.get("/ajustes/").text).group(1)

    def test_solo_it(self):
        r = cliente("ana", ["/PRO/Tareas/Admin"]).get("/ajustes/")
        self.assertEqual(r.status_code, 403)
        self.assertIn("solo para IT", r.text)

    def test_sin_sesion_al_login(self):
        r = cliente().get("/ajustes/", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/ajustes/entrar?next=/ajustes/")

    def test_desde_dev_se_va_al_de_pro(self):
        r = cliente("henry", ["/IT"], entorno="dev").get("/ajustes/", follow_redirects=False)
        self.assertEqual(r.headers["location"], "https://pro.ejemplo.com/ajustes/")

    def test_it_ve_las_tarjetas(self):
        r = cliente("henry", ["/IT"]).get("/ajustes/")
        self.assertEqual(r.status_code, 200)
        for t in ("Tareas", "Inventario", "Lin-AI", "aún sin manifiesto", "Pasar a PRO"):
            self.assertIn(t, r.text)

    def test_post_sin_csrf_no_vale(self):
        r = cliente("henry", ["/IT"]).post("/ajustes/apps/grupo/", data={"ruta": "linai", "grupo": "Lin-AI"})
        self.assertEqual(r.status_code, 403)

    def test_post_de_quien_no_es_it_no_vale(self):
        c = cliente("ana", ["/PRO/Tareas"])
        r = c.post("/ajustes/apps/grupo/", data={"ruta": "linai", "grupo": "Lin-AI", "csrf": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(list(Path(TMP, "peticiones").glob("*.json")), [])

    def test_grupo_deja_peticion(self):
        c = cliente("henry", ["/IT"])
        r = c.post("/ajustes/apps/grupo/", data={"ruta": "linai", "grupo": "Lin-AI", "csrf": self.csrf(c)}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        [p] = Path(TMP, "peticiones").glob("*.json")
        datos = json.loads(p.read_text())
        self.assertEqual((datos["accion"], datos["datos"], datos["quien"]), ("grupo", {"id": "linai", "grupo": "Lin-AI"}, "henry"))

    def test_pasar_a_pro_workflow(self):
        c = cliente("henry", ["/IT"])
        def ver(ent, *_a):
            return {"ok": True, "version": "sha-2222222" if ent == "dev" else "v1.0.0", "detalle": ""}
        with mock.patch.object(github, "pasar_a_pro") as pap, mock.patch.object(panel, "version_en", ver):
            c.post("/ajustes/pasar-a-pro/", data={"ruta": "tareas", "csrf": self.csrf(c)})
        d, v = pap.call_args[0]
        self.assertEqual((d["workflow"], v), ("despliegue.yml", "sha-2222222"))

    def test_pasar_a_pro_servidor_usa_la_version_de_dev(self):
        c = cliente("henry", ["/IT"])
        c.post("/ajustes/pasar-a-pro/", data={"ruta": "inventario", "csrf": self.csrf(c)})
        [p] = Path(TMP, "peticiones").glob("*.json")
        self.assertEqual(json.loads(p.read_text())["datos"], {"id": "inventario", "entorno": "pro", "version": "sha-1111111"})

    def test_poner_anterior(self):
        c = cliente("henry", ["/IT"])
        with mock.patch.object(github, "lanzar") as lanzar:
            c.post("/ajustes/poner/", data={"ruta": "tareas", "entorno": "dev", "version": "anterior", "csrf": self.csrf(c)})
        self.assertEqual(lanzar.call_args[0][1:], ("dev", "anterior"))

    def test_pasar_a_pro_no_hace_nada_si_pro_ya_es_igual(self):
        c = cliente("henry", ["/IT"])
        with mock.patch.object(github, "pasar_a_pro") as pap:
            r = c.post("/ajustes/pasar-a-pro/", data={"ruta": "tareas", "csrf": self.csrf(c)})
        pap.assert_not_called()
        self.assertIn("PRO ya tiene sha-2222222", r.text)
        self.assertIn("PRO ya tiene lo mismo que DEV", r.text)

    def test_despliegue_en_curso_se_ve_en_otra_pestana_y_no_deja_otro(self):
        a, b = cliente("henry", ["/IT"]), cliente("luis", ["/IT"])
        with mock.patch.object(github, "lanzar") as lanzar:
            a.post("/ajustes/poner/", data={"ruta": "tareas", "entorno": "pro", "version": "v9.9.9", "csrf": self.csrf(a)})
            # otra persona (otra sesion) abre Ajustes: lo ve, y no puede pedir otro encima
            r = b.get("/ajustes/")
            self.assertIn("Poniendo <b>v9.9.9</b> en <b>PRO</b>", r.text)
            self.assertIn("pedido por henry", r.text)
            r = b.post("/ajustes/poner/", data={"ruta": "tareas", "entorno": "dev", "version": "anterior", "csrf": self.csrf(b)})
            self.assertIn("ya se está desplegando", r.text)
        self.assertEqual(lanzar.call_count, 1)

    def test_en_curso_acaba_cuando_github_termina(self):
        c = cliente("henry", ["/IT"])
        with mock.patch.object(github, "lanzar"):
            c.post("/ajustes/poner/", data={"ruta": "tareas", "entorno": "pro", "version": "anterior", "csrf": self.csrf(c)})
        desde = en_curso.leer()["tareas"]["desde"]
        guardado.guardar_token("github_pat_" + "b" * 40)
        mal = [{"id": 1, "titulo": "x", "estado": "completed", "resultado": "failure", "url": "https://gh/run/1",
                "evento": "workflow_dispatch", "creada": desde, "fecha": ""}]
        with mock.patch.object(github, "ejecuciones", return_value=mal), mock.patch.object(github, "pendientes", return_value=[]):
            r = c.get("/ajustes/")
            self.assertIn("Falló poner", r.text)
            self.assertIn("https://gh/run/1", r.text)
        bien = [{**mal[0], "id": 2, "resultado": "success"}]
        with mock.patch.object(github, "lanzar"):
            c.post("/ajustes/poner/", data={"ruta": "tareas", "entorno": "pro", "version": "anterior", "csrf": self.csrf(c)})
        with mock.patch.object(github, "ejecuciones", return_value=bien), mock.patch.object(github, "pendientes", return_value=[]):
            self.assertNotIn("Poniendo", c.get("/ajustes/").text)
        self.assertEqual(en_curso.leer(), {})

    def test_si_github_falla_al_pedir_no_se_queda_en_curso(self):
        c = cliente("henry", ["/IT"])
        with mock.patch.object(github, "lanzar", side_effect=github.GitHubError("no")):
            c.post("/ajustes/poner/", data={"ruta": "tareas", "entorno": "pro", "version": "v1.0.0", "csrf": self.csrf(c)})
        self.assertEqual(en_curso.leer(), {})

    def test_anadir_lee_el_manifiesto(self):
        c = cliente("henry", ["/IT"])
        apps = [{"ruta": "linai", "nombre": "Lin-AI"}, {"ruta": "paginas", "nombre": "Páginas"}]
        with mock.patch.object(github, "leer_manifiesto", return_value=apps):
            c.post("/ajustes/apps/anadir/", data={"repo": "https://github.com/yo/spsil-proyecto", "csrf": self.csrf(c)})
        pets = [json.loads(p.read_text()) for p in sorted(Path(TMP, "peticiones").glob("*.json"))]
        self.assertEqual([(p["accion"], p["datos"]["repo"], p["datos"]["manifiesto"]["ruta"]) for p in pets],
                         [("registrar", "yo/spsil-proyecto", "linai"), ("registrar", "yo/spsil-proyecto", "paginas")])

    def test_token(self):
        c = cliente("henry", ["/IT"])
        c.post("/ajustes/github/", data={"token": "github_pat_" + "a" * 40, "csrf": self.csrf(c)})
        self.assertEqual(guardado.token(), "github_pat_" + "a" * 40)
        self.assertNotIn("github_pat_", Path(TMP, "datos", "ajustes.json").read_text())

    def test_agente_parado_se_avisa(self):
        poner_catalogo({**CATALOGO, "latido": "2001-01-01T00:00:00Z"})
        self.assertIn("no está atendiendo", cliente("henry", ["/IT"]).get("/ajustes/").text)


class Importar(unittest.TestCase):
    def test_token_de_antes(self):
        import base64
        import hashlib
        import os as _os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        sal, nonce = _os.urandom(16), _os.urandom(12)
        k = hashlib.pbkdf2_hmac("sha256", b"secreto-de-tareas", sal, 200_000, dklen=32)
        cifrado = base64.b64encode(sal + nonce + AESGCM(k).encrypt(nonce, b"github_pat_viejo", None)).decode()
        self.assertEqual(importar.descifrar_de_antes("secreto-de-tareas", cifrado), "github_pat_viejo")


class GitHub(unittest.TestCase):
    def test_aprueba_lo_que_espera_para_pro(self):
        d = {"repo": "yo/tareas", "workflow": "despliegue.yml"}
        respuestas = [
            {"workflow_runs": [{"id": 7, "status": "waiting", "created_at": "2030-01-01T00:00:00Z"}]},
            [{"environment": {"name": "pro", "id": 3}, "current_user_can_approve": True}],
            None,
        ]
        with mock.patch.object(github, "llamar", side_effect=respuestas) as llamar:
            self.assertTrue(github.aprobar_cuando_llegue(d, "2029-01-01T00:00:00Z", dormir=lambda s: None))
        self.assertEqual(llamar.call_args[0][:2], ("POST", "/repos/yo/tareas/actions/runs/7/pending_deployments"))
        self.assertEqual(llamar.call_args[1]["json"]["state"], "approved")

    def test_manifiesto_con_varias_apps(self):
        import base64
        cuerpo = {"content": base64.b64encode(json.dumps({"apps": [{"ruta": "a"}, {"ruta": "b"}]}).encode()).decode()}
        with mock.patch.object(github, "llamar", return_value=cuerpo):
            self.assertEqual([a["ruta"] for a in github.leer_manifiesto("yo/x")], ["a", "b"])


SPSIL = {"ruta": "spsil", "id": "spsil", "nombre": "SPSIL", "grupo": "SPSIL", "principal": True, "dominio": "spsil.ejemplo.com",
         "logo": "spsil-logo.webp", "version": "version/", "despliegue": {"tipo": "ninguno"},
         "estado_dominio": {"fecha": "2030-01-01T10:00:00Z", "partes": {"dns": {"ok": True}, "proxy": {"ok": False, "detalle": "va a spsil-portal"}}}}
TAREAS_SPSIL = {"ruta": "tareas", "id": "spsil.tareas", "dentro": "spsil", "nombre": "Tareas SPSIL", "grupo": "Tareas",
                "version": "version/", "repo": "yo/tareas-spsil",
                "donde": {"dev": {"host": "dev.ejemplo.com", "camino": "/spsil/tareas/"},
                          "pro": {"host": "spsil.ejemplo.com", "camino": "/tareas/"}},
                "despliegue": {"tipo": "workflow", "repo": "yo/tareas-spsil", "workflow": "despliegue.yml", "rama": "main",
                               "campo_entorno": "entorno", "campo_version": "version", "anterior": True}}
CON_SPSIL = {**CATALOGO, "apps": CATALOGO["apps"] + [SPSIL, TAREAS_SPSIL], "entradas": {
    "dev": CATALOGO["entradas"]["dev"] + [
        {"ruta": "spsil", "id": "spsil", "nombre": "SPSIL", "principal": True, "camino": "/spsil/", "host": "dev.ejemplo.com",
         "grupos": ["DEV/SPSIL"], "salud": "version/"},
        {"ruta": "tareas", "id": "spsil.tareas", "nombre": "Tareas SPSIL", "dentro": "spsil", "camino": "/spsil/tareas/",
         "host": "dev.ejemplo.com", "grupos": ["DEV/SPSIL/Tareas"], "salud": "version/"}],
    "pro": CATALOGO["entradas"]["pro"] + [
        {"ruta": "spsil", "id": "spsil", "nombre": "SPSIL", "principal": True, "camino": "/", "host": "spsil.ejemplo.com",
         "grupos": ["PRO/SPSIL"], "salud": "version/"},
        {"ruta": "tareas", "id": "spsil.tareas", "nombre": "Tareas SPSIL", "dentro": "spsil", "camino": "/tareas/",
         "host": "spsil.ejemplo.com", "grupos": ["PRO/SPSIL/Tareas"], "salud": "version/"}]}}


def cliente_spsil(usuario=None, grupos=()):
    """Por su dominio: la entrada de PRO pone X-Entorno pro y X-Principal spsil."""
    c = TestClient(panel.app, base_url="http://spsil.ejemplo.com")
    c.headers.update({"X-Entorno": "pro", "X-Principal": "spsil"})
    if usuario:
        datos = {"usuario": usuario, "grupos": sorted(g.strip("/") for g in grupos), "id_token": "tok"}
        with mock.patch.object(panel, "canjear_codigo", return_value=datos) as canje:
            r = c.get("/ajustes/entrar?next=/", follow_redirects=False)
            from urllib.parse import parse_qs, urlparse
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            c.get(f"/ajustes/entrar/vuelta?code=abc&state={state}", follow_redirects=False)
            c.canje = canje
    return c


@mock.patch.dict(os.environ, {"KEYCLOAK_CLIENTE_SPSIL": "servidor-spsil", "KEYCLOAK_SECRETO_SPSIL": "s2"})
class Principal(unittest.TestCase):
    def setUp(self):
        poner_catalogo(CON_SPSIL)
        for p in Path(TMP, "peticiones").glob("*"):
            p.unlink()

    def test_su_dominio_usa_su_cliente_y_vuelve_a_su_dominio(self):
        r = cliente_spsil().get("/ajustes/entrar?next=/", follow_redirects=False)
        self.assertIn("client_id=servidor-spsil", r.headers["location"])
        self.assertIn("https%3A%2F%2Fspsil.ejemplo.com%2Fajustes%2Fentrar%2Fvuelta", r.headers["location"])
        c = cliente_spsil("susana", ["/PRO/SPSIL/Tareas"])
        self.assertEqual(c.canje.call_args.args[2], ("servidor-spsil", "s2"))
        r = c.get("/ajustes/salir", follow_redirects=False)
        self.assertIn("client_id=servidor-spsil", r.headers["location"])
        self.assertIn("post_logout_redirect_uri=https%3A%2F%2Fspsil.ejemplo.com%2F", r.headers["location"])

    def test_su_portada_solo_ensena_sus_subapps(self):
        r = cliente_spsil("susana", ["/PRO/SPSIL/Tareas"]).get("/portada/")
        self.assertIn("Tareas SPSIL", r.text)
        self.assertIn('href="/tareas/"', r.text)
        self.assertIn('src="/spsil-logo.webp"', r.text)
        self.assertNotIn("Inventario", r.text)
        self.assertNotIn(">Tareas<", r.text)
        r = cliente_spsil("ana", ["/PRO/Tareas"]).get("/portada/")   # lo vuestro no da SPSIL
        self.assertIn("No tienes acceso", r.text)

    def test_en_dev_esta_en_su_ruta_y_en_la_portada_de_dev_sale_una_tarjeta(self):
        c = cliente("ana", ["/DEV/SPSIL/Tareas"], entorno="dev")
        r = c.get("/portada/")
        self.assertIn('href="/spsil/"', r.text)
        self.assertNotIn("Tareas SPSIL", r.text)
        r = c.get("/portada/", headers={"X-Principal": "spsil"})
        self.assertIn('href="/spsil/tareas/"', r.text)

    def test_ajustes_la_agrupa_con_su_dominio(self):
        with mock.patch.object(panel, "version_en", sin_versiones):
            r = cliente("henry", ["/IT"]).get("/ajustes/")
        self.assertIn("App principal", r.text)
        self.assertIn("Tareas SPSIL", r.text)
        self.assertIn("https://spsil.ejemplo.com/tareas/", r.text)
        self.assertIn("Sincronizar", r.text)
        self.assertIn("va a spsil-portal", r.text)
        self.assertIn('name="id" value="spsil.tareas"', r.text)

    def test_anadir_dentro(self):
        c = cliente("henry", ["/IT"])
        with mock.patch.object(panel, "version_en", sin_versiones):
            csrf = __import__("re").search(r'name="csrf" value="([^"]+)"', c.get("/ajustes/").text).group(1)
        with mock.patch.object(github, "leer_manifiesto", return_value=[{"ruta": "linai"}, {"ruta": "paginas"}]):
            c.post("/ajustes/apps/anadir/", data={"repo": "yo/spsil-proyecto", "dentro": "spsil", "solo": "linai",
                                                  "contenedor_pro": "spsil-linai", "csrf": csrf})
        pets = [json.loads(p.read_text())["datos"] for p in Path(TMP, "peticiones").glob("*.json")]
        self.assertEqual(len(pets), 1)
        self.assertEqual((pets[0]["dentro"], pets[0]["manifiesto"]["ruta"], pets[0]["contenedor_pro"]), ("spsil", "linai", "spsil-linai"))

    def test_estaticos_sin_salirse(self):
        d = Path(TMP, "estado", "estaticos", "spsil")
        d.mkdir(parents=True, exist_ok=True)
        (d / "spsil-logo.webp").write_bytes(b"RIFF")
        c = TestClient(panel.app)
        r = c.get("/ajustes/estaticos/spsil/spsil-logo.webp")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/webp"))
        for malo in ("/ajustes/estaticos/spsil/..%2Fcatalogo.json", "/ajustes/estaticos/spsil/.oculto",
                     "/ajustes/estaticos/../estado/x.webp"):
            self.assertEqual(c.get(malo).status_code, 404, malo)


if __name__ == "__main__":
    unittest.main()


@mock.patch.object(panel, "version_en", sin_versiones)
class Api(unittest.TestCase):
    """La API con clave: ver las apps y desplegar sin sesion, por defecto solo en DEV."""

    def setUp(self):
        poner_catalogo()
        for p in Path(TMP, "peticiones").glob("*"):
            p.unlink()
        for f in ("en_curso.json", "api.json", "api.log"):
            Path(TMP, "datos", f).unlink(missing_ok=True)

    def clave(self, **kw):
        return claves.crear(kw.get("nombre", "Claude"), kw.get("entornos", ["dev"]), kw.get("apps", []), "henry",
                            kw.get("dias", 90))[0]

    def api(self, clave=None):
        c = cliente(entorno="pro")
        if clave:
            c.headers["Authorization"] = f"Bearer {clave}"
        return c

    def test_sin_clave_o_clave_mala_no_entra(self):
        self.assertEqual(self.api().get("/ajustes/api/v1/apps").status_code, 401)
        k = self.clave()
        self.assertEqual(self.api(k[:-2] + "xx").get("/ajustes/api/v1/apps").status_code, 401)
        self.assertEqual(self.api("srv_00000000_" + "a" * 43).get("/ajustes/api/v1/yo").status_code, 401)
        self.assertTrue(any(r["accion"] == "rechazada" for r in claves.registro()))

    def test_la_sesion_de_it_no_vale_como_clave(self):
        c = cliente("henry", ["/IT"])
        r = c.post("/ajustes/api/v1/apps/inventario/desplegar", json={"entorno": "dev", "version": "sha-3333333"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(list(Path(TMP, "peticiones").glob("*.json")), [])

    def test_caducada_no_vale(self):
        k = self.clave()
        datos = json.loads(Path(TMP, "datos", "api.json").read_text())
        datos["claves"][0]["caduca"] = "2001-01-01T00:00:00Z"
        Path(TMP, "datos", "api.json").write_text(json.dumps(datos))
        self.assertEqual(self.api(k).get("/ajustes/api/v1/yo").status_code, 401)

    def test_solo_se_guarda_el_hash(self):
        k = self.clave()
        self.assertNotIn(k.split("_", 2)[2], Path(TMP, "datos", "api.json").read_text())

    def test_yo_y_apps(self):
        k = self.clave()
        r = self.api(k).get("/ajustes/api/v1/yo").json()
        self.assertEqual((r["clave"]["nombre"], r["clave"]["entornos"]), ("Claude", ["dev"]))
        apps = {a["id"]: a for a in self.api(k).get("/ajustes/api/v1/apps").json()["apps"]}
        self.assertEqual(set(apps), {"tareas", "inventario"})
        self.assertTrue(apps["inventario"]["entornos"]["dev"]["puede_desplegar"])
        self.assertFalse(apps["inventario"]["entornos"]["pro"]["puede_desplegar"])

    def test_desplegar_en_dev_deja_la_peticion_y_lo_apunta(self):
        k = self.clave()
        r = self.api(k).post("/ajustes/api/v1/apps/inventario/desplegar", json={"entorno": "dev", "version": "sha-3333333"})
        self.assertEqual(r.status_code, 202, r.text)
        [p] = Path(TMP, "peticiones").glob("*.json")
        pet = json.loads(p.read_text())
        self.assertEqual((pet["accion"], pet["datos"], pet["quien"]),
                         ("desplegar", {"id": "inventario", "entorno": "dev", "version": "sha-3333333"}, "api:Claude"))
        [ultimo] = [x for x in claves.registro() if x["accion"] == "desplegar"]
        self.assertTrue(ultimo["ok"])
        # mientras, nadie pide otro encima (ni la API ni el boton)
        r = self.api(k).post("/ajustes/api/v1/apps/inventario/desplegar", json={"entorno": "dev", "version": "anterior"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("pedido por api:Claude", cliente("luis", ["/IT"]).get("/ajustes/").text)

    def test_workflow_lanza_github(self):
        k = self.clave()
        with mock.patch.object(github, "lanzar") as lanzar:
            r = self.api(k).post("/ajustes/api/v1/apps/tareas/desplegar", json={"entorno": "dev", "version": "anterior"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(lanzar.call_args[0][1:], ("dev", "anterior"))

    def test_pro_no_con_una_clave_de_dev(self):
        k = self.clave()
        with mock.patch.object(github, "lanzar") as lanzar, mock.patch.object(github, "pasar_a_pro") as pap:
            r1 = self.api(k).post("/ajustes/api/v1/apps/tareas/desplegar", json={"entorno": "pro", "version": "v1"})
            r2 = self.api(k).post("/ajustes/api/v1/apps/tareas/pasar-a-pro")
        self.assertEqual((r1.status_code, r2.status_code), (403, 403))
        lanzar.assert_not_called()
        pap.assert_not_called()
        self.assertEqual(en_curso.leer(), {})
        self.assertFalse(any(x["ok"] for x in claves.registro() if x["accion"] in ("desplegar", "pasar_a_pro")))

    def test_pro_con_una_clave_que_lo_permite(self):
        k = self.clave(entornos=["dev", "pro"])
        r = self.api(k).post("/ajustes/api/v1/apps/inventario/pasar-a-pro")
        self.assertEqual(r.status_code, 202, r.text)
        [p] = Path(TMP, "peticiones").glob("*.json")
        self.assertEqual(json.loads(p.read_text())["datos"], {"id": "inventario", "entorno": "pro", "version": "sha-1111111"})

    def test_clave_de_una_app_no_ve_las_demas(self):
        k = self.clave(apps=["inventario"])
        self.assertEqual([a["id"] for a in self.api(k).get("/ajustes/api/v1/apps").json()["apps"]], ["inventario"])
        self.assertEqual(self.api(k).get("/ajustes/api/v1/apps/tareas").status_code, 404)
        r = self.api(k).post("/ajustes/api/v1/apps/tareas/desplegar", json={"entorno": "dev", "version": "v1"})
        self.assertEqual(r.status_code, 404)

    def test_datos_malos(self):
        k = self.clave()
        r = self.api(k).post("/ajustes/api/v1/apps/inventario/desplegar", json={"entorno": "dev", "version": "a b; rm"})
        self.assertEqual(r.status_code, 400)
        r = self.api(k).post("/ajustes/api/v1/apps/inventario/desplegar", content=b"no es json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(list(Path(TMP, "peticiones").glob("*.json")), [])

    def test_traspaso_trae_apps_y_no_la_clave(self):
        k = self.clave()
        r = self.api(k).get("/ajustes/api/v1/traspaso")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/markdown", r.headers["content-type"])
        self.assertIn("`inventario`", r.text)
        self.assertIn("https://pro.ejemplo.com/ajustes/api/v1", r.text)
        self.assertIn("no puede tocar PRO", r.text)
        self.assertNotIn(k, r.text)

    def test_estado(self):
        k = self.clave()
        r = self.api(k).get("/ajustes/api/v1/estado").json()
        self.assertTrue(r["agente_vivo"])
        self.assertEqual(r["en_cola"], 0)

    def test_ajustes_crea_ensena_una_vez_y_quita(self):
        import re
        c = cliente("henry", ["/IT"])
        csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/ajustes/").text).group(1)
        r = c.post("/ajustes/api/claves/", data={"nombre": "Claude", "dias": "30", "csrf": csrf})
        texto = re.search(r"(srv_[0-9a-f]{8}_[A-Za-z0-9_-]+)", r.text).group(1)
        self.assertEqual(claves.comprobar(texto)["entornos"], ["dev"])
        self.assertNotIn(texto, c.get("/ajustes/").text)
        cid = texto.split("_")[1]
        c.post("/ajustes/api/claves/quitar/", data={"id": cid, "csrf": csrf})
        self.assertIsNone(claves.comprobar(texto))
        self.assertEqual(self.api(texto).get("/ajustes/api/v1/yo").status_code, 401)

    def test_crear_clave_pide_csrf_e_it(self):
        r = cliente("ana", ["/PRO/Tareas/Admin"]).post("/ajustes/api/claves/", data={"nombre": "x", "csrf": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(claves.listar(), [])
