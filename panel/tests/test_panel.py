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

from panel import app as panel, en_curso, github, guardado, importar  # noqa: E402

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
        self.assertEqual((datos["accion"], datos["datos"], datos["quien"]), ("grupo", {"ruta": "linai", "grupo": "Lin-AI"}, "henry"))

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
        self.assertEqual(json.loads(p.read_text())["datos"], {"ruta": "inventario", "entorno": "pro", "version": "sha-1111111"})

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


if __name__ == "__main__":
    unittest.main()
