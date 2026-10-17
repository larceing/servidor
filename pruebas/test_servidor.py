# -*- coding: utf-8 -*-
"""Pruebas de servidor.py sin tocar Docker ni Keycloak (se simulan).
  python -m unittest discover -s pruebas -v"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import servidor as S  # noqa: E402

TAREAS_CONF = """# app: Gestor de Tareas
# salud: version/
location = /tareas { return 308 /tareas/; }
location /tareas/ {
  set $destino http://tareas-dev:8000;
  proxy_pass $destino;
}
"""
LINAI_CONF = """# app: Lin-AI
# grupo: DEV/Lin-AI
location /linai/ { proxy_pass http://linai-dev:8000/; }
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / "servidor"
        self.legado = self.tmp / "tareas" / "entrada"
        for ent in S.ENTORNOS:
            (self.legado / ent / "apps").mkdir(parents=True)
        (self.legado / "dev" / "apps" / "tareas.conf").write_text(TAREAS_CONF)
        (self.legado / "dev" / "apps" / "linai.conf").write_text(LINAI_CONF)
        (self.legado / "dev" / "apps" / "MAL nombre.conf").write_text("x")
        (self.legado / "grupos").write_text("paginas Paginas\n")
        self.dir.mkdir()
        (self.dir / "servidor.env").write_text(
            f"DOMINIO_DEV=dev.ejemplo.com\nDOMINIO_PRO=pro.ejemplo.com\nLEGADO={self.legado}\n"
            "KEYCLOAK_ISSUER=https://u.ejemplo.com/auth/realms/general\nENTORNOS_GESTIONADOS=dev\nPANEL_CLAVE=x\n")
        self.p = mock.patch.object(S, "DIR", self.dir)
        self.p.start()
        S.preparar_carpetas()
        S._RECHAZADAS.clear()

    def tearDown(self):
        self.p.stop()

    def alta(self, **m):
        base = {"ruta": "inventario", "nombre": "Inventario", "grupo": "Inventario",
                "despliegue": {"tipo": "servidor", "imagen": "ghcr.io/yo/inventario"}}
        S.guardar_registro(S.validar_manifiesto({**base, **m}, "yo/inventario"), "yo/inventario")


class Manifiesto(Base):
    def test_minimo_y_valores_por_defecto(self):
        m = S.validar_manifiesto({"ruta": "linai", "nombre": "Lin-AI", "grupo": "/DEV/Lin-AI",
                                  "despliegue": {"workflow": "linai.yml"}}, "yo/spsil")
        self.assertEqual(m["grupo"], "Lin-AI")
        self.assertEqual(m["contenedor"], "{ruta}-{entorno}")
        self.assertEqual((m["prefijo"], m["entrada"], m["version"]), ("quitar", "servidor", "version/"))
        self.assertEqual(m["despliegue"]["repo"], "yo/spsil")
        self.assertEqual(m["despliegue"]["campo_entorno"], "entorno")

    def test_rechaza_lo_que_no_cuadra(self):
        malos = [{"ruta": "ajustes"}, {"ruta": "A"}, {"ruta": "x;rm"}, {"ruta": "app", "contenedor": "a b"},
                 {"ruta": "app", "puerto": 0}, {"ruta": "app", "prefijo": "otro"},
                 {"ruta": "app", "despliegue": {"tipo": "workflow", "workflow": "../x.yml", "repo": "a/b"}},
                 {"ruta": "app", "despliegue": {"tipo": "servidor", "imagen": "img:latest"}},
                 {"ruta": "app", "nombre": "x'; alert(1)"}, {"ruta": "app", "grupo": "a;b"},
                 {"ruta": "app", "version": "a b"}]
        for m in malos:
            with self.assertRaises(S.Error, msg=m):
                S.validar_manifiesto(m)

    def test_servidor_lleva_cliente_de_keycloak(self):
        m = S.validar_manifiesto({"ruta": "app", "despliegue": {"tipo": "servidor", "imagen": "ghcr.io/a/b"}})
        self.assertEqual(m["keycloak"], {"cliente": "{ruta}-{entorno}"})


class Entrada(Base):
    def test_default_manda_portada_y_ajustes_al_panel(self):
        f = S.ficheros_entrada("dev")["default.conf"]
        self.assertIn("proxy_pass $panel/portada/;", f)
        self.assertIn("proxy_set_header X-Entorno dev;", f)
        self.assertIn("include /etc/nginx/conf.d/legado/*.conf;", f)

    def test_legado_se_copia_tal_cual(self):
        f = S.ficheros_entrada("dev")
        self.assertEqual(f["legado/tareas.conf"], TAREAS_CONF)
        self.assertEqual(f["legado/linai.conf"], LINAI_CONF)
        self.assertFalse(any("MAL" in k for k in f))
        self.assertFalse(any(k.startswith("legado/") for k in S.ficheros_entrada("pro")))

    def test_app_del_servidor_quita_su_conf_de_antes(self):
        self.alta(ruta="tareas", nombre="Tareas", prefijo="conservar", contenedor="tareas-{entorno}",
                  despliegue={"tipo": "workflow", "workflow": "despliegue.yml"})
        f = S.ficheros_entrada("dev")
        self.assertNotIn("legado/tareas.conf", f)
        self.assertIn("set $destino tareas-dev:8000;", f["apps/tareas.conf"])
        self.assertNotIn("rewrite", f["apps/tareas.conf"])
        self.assertIn("X-Forwarded-Prefix /tareas;", f["apps/tareas.conf"])

    def test_propia_mantiene_su_conf(self):
        self.alta(ruta="linai", nombre="Lin-AI", entrada="propia", despliegue={"tipo": "workflow", "workflow": "linai.yml"})
        f = S.ficheros_entrada("dev")
        self.assertIn("legado/linai.conf", f)
        self.assertNotIn("apps/linai.conf", f)

    def test_quitar_prefijo_y_dominio(self):
        self.alta()
        reg = json.loads((self.dir / "registro" / "inventario.json").read_text())
        reg["dominio"] = "inventario.ejemplo.com"
        (self.dir / "registro" / "inventario.json").write_text(json.dumps(reg))
        dev, pro = S.ficheros_entrada("dev"), S.ficheros_entrada("pro")
        self.assertIn("rewrite ^/inventario/(.*)$ /$1 break;", dev["apps/inventario.conf"])
        self.assertNotIn("dominio-inventario.conf", dev)
        self.assertIn("server_name inventario.ejemplo.com;", pro["dominio-inventario.conf"])
        self.assertIn("set $destino inventario-pro:8000;", pro["dominio-inventario.conf"])

    def test_aplicar_vuelve_atras_si_nginx_no_la_acepta(self):
        with mock.patch.object(S, "nginx_valida"), mock.patch("subprocess.run"):
            self.assertTrue(S.aplicar_entrada("dev"))
        antes = S._leer_carpeta(self.dir / "entrada" / "dev")
        self.alta()
        with mock.patch.object(S, "nginx_valida", side_effect=S.Error("roto")), mock.patch("subprocess.run"):
            with self.assertRaises(S.Error):
                S.aplicar_entrada("dev")
        self.assertEqual(S._leer_carpeta(self.dir / "entrada" / "dev"), antes)

    def test_no_reintenta_lo_que_nginx_rechazo(self):
        with mock.patch.object(S, "nginx_valida", side_effect=S.Error("roto")) as v, mock.patch("subprocess.run"):
            with self.assertRaises(S.Error):
                S.aplicar_entrada("dev")
            self.assertFalse(S.aplicar_entrada("dev"))
        self.assertEqual(v.call_count, 1)

    def test_sin_cambios_no_recarga(self):
        with mock.patch.object(S, "nginx_valida") as v, mock.patch("subprocess.run"):
            S.aplicar_entrada("dev")
            self.assertFalse(S.aplicar_entrada("dev"))
        self.assertEqual(v.call_count, 1)


class Catalogo(Base):
    def test_grupos_por_entorno(self):
        self.alta()
        c = S.catalogo()
        dev = {a["ruta"]: a for a in c["entradas"]["dev"]}
        self.assertEqual(dev["linai"]["grupo"], "DEV/Lin-AI")
        self.assertEqual(dev["tareas"]["grupo"], "")
        self.assertEqual(dev["inventario"]["grupo"], "DEV/Inventario")
        self.assertEqual({a["ruta"] for a in c["entradas"]["pro"]}, {"inventario"})
        self.assertEqual({s["ruta"] for s in c["sin_alta"]}, {"tareas", "linai"})

    def test_propia_sin_ruta_en_ese_entorno_no_sale(self):
        self.alta(ruta="linai", nombre="Lin-AI", grupo="Lin-AI", entrada="propia",
                  despliegue={"tipo": "workflow", "workflow": "linai.yml"})
        c = S.catalogo()
        self.assertIn("linai", {a["ruta"] for a in c["entradas"]["dev"]})
        self.assertNotIn("linai", {a["ruta"] for a in c["entradas"]["pro"]})


class Peticiones(Base):
    def pedir(self, accion, datos, pid="20260101000000-aaaa"):
        (self.dir / "peticiones" / f"{pid}.json").write_text(json.dumps({"accion": accion, "datos": datos, "quien": "henry"}))
        S.procesar_peticiones()
        return json.loads((self.dir / "estado" / "resultados" / f"{pid}.json").read_text())

    @mock.patch.object(S, "aplicar_entrada")
    @mock.patch.object(S, "kc_entrar")
    @mock.patch.object(S, "asegurar_grupos")
    @mock.patch.object(S, "asegurar_cliente", return_value="secreto")
    def test_registrar_crea_grupos_cliente_y_ruta(self, cli, grupos, _e, aplicar):
        r = self.pedir("registrar", {"repo": "yo/inventario", "manifiesto": {
            "ruta": "inventario", "nombre": "Inventario", "grupo": "Inventario",
            "despliegue": {"tipo": "servidor", "imagen": "ghcr.io/yo/inventario"}}})
        self.assertTrue(r["ok"], r["salida"])
        grupos.assert_called_once_with("Inventario")
        self.assertEqual([c.args[0] for c in cli.call_args_list], ["inventario-dev", "inventario-pro"])
        self.assertEqual(cli.call_args_list[1].args[2], ["https://pro.ejemplo.com/inventario"])
        self.assertEqual(S.leer_env(self.dir / "secretos" / "inventario-pro.env")["KEYCLOAK_CLIENT_SECRET"], "secreto")
        aplicar.assert_called_with("dev")
        self.assertIn("inventario", S.registro())

    def test_peticion_mala_no_rompe(self):
        r = self.pedir("borrarlo-todo", {})
        self.assertFalse(r["ok"])
        r = self.pedir("registrar", {"manifiesto": {"ruta": "../x"}}, pid="20260101000001-bbbb")
        self.assertFalse(r["ok"])
        self.assertEqual(S.registro(), {})
        self.assertEqual(list((self.dir / "peticiones").glob("*")), [])

    @mock.patch.object(S, "aplicar_entrada")
    @mock.patch.object(S, "kc_entrar")
    @mock.patch.object(S, "asegurar_grupos")
    def test_grupo_de_una_app_sin_alta(self, grupos, _e, _a):
        r = self.pedir("grupo", {"ruta": "tareas", "grupo": "PRO/Tareas"})
        self.assertTrue(r["ok"], r["salida"])
        grupos.assert_called_once_with("Tareas")
        self.assertEqual(S.grupos_sueltos()["tareas"], "Tareas")
        self.assertEqual({a["ruta"]: a["grupo"] for a in S.catalogo()["entradas"]["dev"]}["tareas"], "DEV/Tareas")

    def test_npm_no_deja_el_email_en_el_resultado(self):
        with mock.patch.object(S, "npm_token", side_effect=S.Error("no entra yo@x.com")):
            r = self.pedir("npm", {"email": "yo@x.com", "clave": "c"})
        self.assertFalse(r["ok"])
        self.assertNotIn("yo@x.com", r["salida"])
        self.assertFalse((self.dir / ".npm.env").exists())


class Versiones(Base):
    def test_anterior_no_rebota(self):
        h = self.tmp / "h.log"
        h.write_text("t\tv1\tok\tpanel\nt\tv2\tok\tpanel\nt\tv3\tfallo\tpanel\n")
        self.assertEqual(S.version_anterior(h, "v2"), "v1")
        h.write_text(h.read_text() + "t\tv1\tok\trollback\nt\tv2\trevertida\t-> v1\n")
        self.assertEqual(S.version_anterior(h, "v1"), "")


class Actualizar(Base):
    def test_pruebas_en_verde(self):
        def respuesta(runs):
            m = mock.MagicMock()
            m.__enter__.return_value = m
            m.read.return_value = json.dumps({"check_runs": runs}).encode()
            return m
        casos = [([], "esperando"), ([{"status": "in_progress"}], "esperando"),
                 ([{"status": "completed", "conclusion": "success"}], "ok"),
                 ([{"status": "completed", "conclusion": "failure"}], "mal")]
        for runs, esperado in casos:
            with mock.patch("urllib.request.urlopen", return_value=respuesta(runs)):
                self.assertEqual(S.pruebas_de("yo/servidor", "abc"), esperado)

    def test_no_se_queda_con_un_commit_que_falla(self):
        llamadas = []

        def run(cmd, **_k):
            llamadas.append(cmd)
            if cmd[-1] == "FETCH_HEAD":
                return "nuevo\n"
            if cmd[-1] == "HEAD":
                return "viejo\n"
            return ""
        with mock.patch.object(S, "run", side_effect=run), mock.patch.object(S, "pruebas_de", return_value="mal"):
            self.assertFalse(S.actualizar())
        self.assertIn("nuevo", S._malas())
        self.assertFalse(any("checkout" in c for c in llamadas))


if __name__ == "__main__":
    unittest.main()
