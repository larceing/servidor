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

    def test_grupo_de_antes_nunca_es_el_grupo_raiz(self):
        # "# grupo: Lin-AI" sin entorno (o con el de otro) no debe dar paso a /Lin-AI
        for linea in ("# grupo: Lin-AI", "# grupo: /Lin-AI", "# grupo: PRO/Lin-AI"):
            (self.legado / "dev" / "apps" / "linai.conf").write_text(LINAI_CONF.replace("# grupo: DEV/Lin-AI", linea))
            dev = {a["ruta"]: a for a in S.catalogo()["entradas"]["dev"]}
            self.assertEqual(dev["linai"]["grupo"], "DEV/Lin-AI", linea)

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
    @mock.patch.object(S, "asegurar_caminos")
    @mock.patch.object(S, "asegurar_cliente", return_value="secreto")
    def test_registrar_crea_grupos_cliente_y_ruta(self, cli, grupos, _e, aplicar):
        r = self.pedir("registrar", {"repo": "yo/inventario", "manifiesto": {
            "ruta": "inventario", "nombre": "Inventario", "grupo": "Inventario",
            "despliegue": {"tipo": "servidor", "imagen": "ghcr.io/yo/inventario"}}})
        self.assertTrue(r["ok"], r["salida"])
        grupos.assert_called_once_with(["DEV/Inventario", "PRO/Inventario"])
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


class Principal(Base):
    """Una app principal (SPSIL) con subapps: pasan por DEV y PRO como todas,
    y PRO puede ir a su dominio."""
    def setUp(self):
        super().setUp()
        S.guardar_registro(S.validar_manifiesto({"ruta": "spsil", "nombre": "SPSIL", "grupo": "SPSIL", "principal": True}))
        S.guardar_registro(S.validar_manifiesto({"ruta": "tareas", "nombre": "Tareas", "grupo": "Tareas", "prefijo": "conservar",
                                                 "despliegue": {"workflow": "despliegue.yml"}}, "yo/tareas"), "yo/tareas")

    def sub(self, ruta="tareas", **extra):
        m = {"ruta": ruta, "nombre": f"{ruta} SPSIL", "grupo": ruta.capitalize(), "prefijo": "conservar", "entrada": "propia",
             "dentro": "spsil", "despliegue": {"workflow": "despliegue.yml"}}
        S.guardar_registro(S.validar_manifiesto(m, "yo/tareas-spsil"), "yo/tareas-spsil", **extra)

    def poner_dominio(self, dom="spsil.ejemplo.com", **extra):
        d = json.loads((self.dir / "registro" / "spsil.json").read_text())
        d.update({"dominio": dom, **extra})
        (self.dir / "registro" / "spsil.json").write_text(json.dumps(d))

    def test_subapp_va_dentro_y_no_choca_con_otra_app(self):
        self.sub()
        reg = S.registro()
        self.assertEqual(set(reg), {"spsil", "tareas", "spsil.tareas"})
        dev = S.ficheros_entrada("dev")
        self.assertIn("location /spsil/tareas/ {", dev["apps/spsil.tareas.conf"])
        self.assertIn("set $destino spsil-tareas-dev:8000;", dev["apps/spsil.tareas.conf"])   # no tareas-dev
        self.assertIn("X-Forwarded-Prefix /spsil/tareas;", dev["apps/spsil.tareas.conf"])
        self.assertIn("proxy_set_header X-Principal spsil;", dev["apps/spsil.conf"])
        self.assertIn("location = /spsil/ {", dev["apps/spsil.conf"])

    def test_grupos_iguales_para_todo_el_servidor(self):
        self.sub()
        c = S.catalogo()
        dev = {a["id"]: a for a in c["entradas"]["dev"]}
        self.assertEqual(dev["spsil.tareas"]["grupos"], ["DEV/SPSIL/Tareas"])
        self.assertEqual(dev["spsil"]["grupos"], ["DEV/SPSIL"])
        self.assertEqual(dev["tareas"]["grupos"], ["DEV/Tareas"])
        self.poner_dominio()
        pro = {a["id"]: a for a in S.catalogo()["entradas"]["pro"]}
        self.assertEqual(pro["spsil.tareas"]["grupos"], ["PRO/SPSIL/Tareas"])   # tambien en su dominio

    def test_contenedor_por_alias_de_npm(self):
        def run(cmd, **k):
            if cmd[:2] == ["docker", "ps"]:
                return "keycloak\nspsil-plataforma-portal-1\n"
            return {"keycloak": '{"proxy_default": {"Aliases": ["keycloak"]}}',
                    "spsil-plataforma-portal-1": '{"proxy_default": {"Aliases": ["spsil-portal", "portal"]}}'}[cmd[-1]]
        with mock.patch.object(S, "run", side_effect=run):
            self.assertEqual(S.contenedor_de("spsil-portal"), "spsil-plataforma-portal-1")
            self.assertEqual(S.contenedor_de("keycloak"), "keycloak")
            self.assertEqual(S.contenedor_de("no-existe"), "")

    def test_pro_en_su_dominio(self):
        self.sub()
        self.sub("linai")
        self.poner_dominio(redirigir={"stock": "/paginas/", "ajustes": "/x/", "auth": "https://users.ejemplo.com"})
        (self.dir / "estado" / "estaticos" / "spsil").mkdir(parents=True)
        (self.dir / "estado" / "estaticos" / "spsil" / "spsil-logo.webp").write_bytes(b"x")
        (self.dir / "estado" / "estaticos" / "spsil" / "js").mkdir()
        S.guardar_registro(S.registro()["spsil.tareas"]["manifiesto"], "yo/tareas-spsil", contenedores={"pro": "spsil-tareas"})
        pro, dev = S.ficheros_entrada("pro"), S.ficheros_entrada("dev")
        d = pro["dominio-spsil.conf"]
        self.assertIn("server_name spsil.ejemplo.com;", d)
        self.assertIn("location /tareas/ {", d)
        self.assertIn("set $destino spsil-tareas:8000;", d)   # el que ya habia
        self.assertIn("X-Forwarded-Prefix /tareas;", d)
        self.assertIn("location /linai/ {", d)
        self.assertIn("proxy_pass $panel/portada/;", d)
        self.assertIn("proxy_set_header X-Principal spsil;", d)
        self.assertIn("location = /spsil-logo.webp {", d)
        self.assertIn("rewrite ^/js/(.*)$ /ajustes/estaticos/spsil/js/$1 break;", d)
        self.assertNotIn("location /marca/", d)   # no la tenia
        self.assertIn("location /stock/ { return 301 /paginas/; }", d)
        self.assertNotIn("/x/", d)
        self.assertIn("location /auth { return 301 https://users.ejemplo.com$request_uri; }", d)
        self.assertNotIn("apps/spsil.tareas.conf", pro)   # en PRO ya solo por su dominio
        self.assertIn("https://spsil.ejemplo.com/$1 permanent;", pro["apps/spsil.conf"])
        self.assertIn("location /spsil/tareas/ {", dev["apps/spsil.tareas.conf"])   # DEV sigue igual
        a = next(x for x in S.catalogo()["apps"] if x["id"] == "spsil.tareas")
        self.assertEqual(a["donde"]["pro"], {"host": "spsil.ejemplo.com", "camino": "/tareas/"})
        self.assertEqual(a["donde"]["dev"], {"host": "dev.ejemplo.com", "camino": "/spsil/tareas/"})

    def test_subapp_sin_principal_no_vale(self):
        with self.assertRaises(S.Error):
            S.validar_manifiesto({"ruta": "x1", "dentro": "spsil", "principal": True})
        S.guardar_registro(S.validar_manifiesto({"ruta": "x1", "dentro": "otra"}))
        self.assertNotIn("otra.x1", S.registro())

    @mock.patch.object(S, "aplicar_entrada")
    @mock.patch.object(S, "kc_entrar")
    @mock.patch.object(S, "asegurar_caminos")
    @mock.patch.object(S, "asegurar_cliente", return_value="s")
    def test_registrar_dentro_desde_el_panel(self, _cli, caminos, _e, _a):
        r = self.pedir_ok("registrar", {"repo": "yo/lin", "dentro": "spsil", "contenedor_pro": "spsil-linai",
                                        "grupos_extra": "Lin-AI Editor",
                                        "manifiesto": {"ruta": "linai", "nombre": "Lin-AI", "grupo": "Lin-AI",
                                                       "despliegue": {"workflow": "linai.yml"}}})
        datos = S.registro()["spsil.linai"]
        self.assertEqual(datos["contenedores"], {"pro": "spsil-linai"})
        self.assertEqual(S.contenedor_en(datos, "dev"), "spsil-linai-dev")
        caminos.assert_called_once_with(["DEV/SPSIL/Lin-AI", "DEV/SPSIL/Lin-AI Editor", "PRO/SPSIL/Lin-AI", "PRO/SPSIL/Lin-AI Editor"])
        self.assertTrue(r["ok"])
        self.pedir_ok("registrar", {"dentro": "tareas", "manifiesto": {"ruta": "x1"}}, pid="20260101000002-cccc", ok=False)
        self.pedir_ok("quitar", {"id": "spsil"}, pid="20260101000003-dddd", ok=False)   # tiene subapps

    def pedir_ok(self, accion, datos, pid="20260101000000-aaaa", ok=True):
        (self.dir / "peticiones" / f"{pid}.json").write_text(json.dumps({"accion": accion, "datos": datos, "quien": "henry"}))
        S.procesar_peticiones()
        r = json.loads((self.dir / "estado" / "resultados" / f"{pid}.json").read_text())
        self.assertEqual(r["ok"], ok, r["salida"])
        return r

    def test_dominio_guarda_a_donde_iba_y_quitarlo_vuelve(self):
        self.sub()
        S.poner_env(self.dir / "servidor.env", {"ENTORNOS_GESTIONADOS": "dev pro"})
        npm = []
        with mock.patch.object(S, "npm_apuntar", side_effect=lambda d, h, p: npm.append((d, h, p)) or {"host": "spsil-portal", "puerto": 80}), \
                mock.patch.object(S, "aplicar_entrada"), mock.patch.object(S, "kc_entrar", side_effect=S.Error("sin kc")), \
                mock.patch.object(S, "copiar_estaticos") as copiar, mock.patch.object(S, "_reiniciar_panel"), \
                mock.patch.object(S, "comprobar_dominio", return_value={"partes": {}}), \
                mock.patch("socket.gethostbyname", return_value="1.2.3.4"):
            r = self.pedir_ok("dominio", {"id": "spsil", "dominio": "spsil.ejemplo.com"})
            self.assertEqual(S.registro()["spsil"]["npm_antes"], {"host": "spsil-portal", "puerto": 80})
            copiar.assert_called_once_with("spsil", "spsil-portal")
            self.pedir_ok("dominio", {"id": "spsil.tareas", "dominio": "otra.ejemplo.com"}, pid="20260101000004-eeee", ok=False)
            self.pedir_ok("quitar_dominio", {"id": "spsil"}, pid="20260101000005-ffff")
        self.assertEqual(npm[0], ("spsil.ejemplo.com", "entrada-pro", 80))
        self.assertEqual(npm[-1], ("spsil.ejemplo.com", "spsil-portal", 80))
        self.assertEqual(S.registro()["spsil"]["dominio"], "")
        self.assertIn("LISTO", r["salida"])

    def test_sincronizar_con_keycloak_pone_el_tema_de_su_login(self):
        self.sub()
        self.poner_dominio(tema="spsil")
        S.poner_env(self.dir / "servidor.env", {"KEYCLOAK_ISSUER": "https://kc.ejemplo.com/realms/r"})
        cli = {"clientId": "servidor-spsil", "attributes": {"login_theme": "keycloak"}}
        puestos = []

        def kc(*args, entrada=None):
            if args[:2] == ("get", "clients"):
                return "id1"
            if args[:2] == ("get", "clients/id1"):
                return json.dumps(cli)
            if args[0] == "update":
                puestos.append(json.loads(entrada))
            return "secreto"
        with mock.patch.object(S, "npm_apuntar", return_value={}), mock.patch.object(S, "aplicar_entrada"), \
                mock.patch.object(S, "kc_entrar"), mock.patch.object(S, "kc", side_effect=kc), \
                mock.patch.object(S, "_reiniciar_panel"), mock.patch.object(S, "comprobar_dominio", return_value={"partes": {}}), \
                mock.patch("socket.gethostbyname", return_value="1.2.3.4"):
            S.sincronizar_dominio("spsil")
        self.assertEqual(puestos[0]["attributes"]["login_theme"], "spsil")
        self.assertIn("https://spsil.ejemplo.com/ajustes/*", puestos[-1]["redirectUris"])

    def test_catalogo_lleva_tema_logo_y_rutas_viejas(self):
        self.poner_dominio(tema="spsil", logo="spsil-logo.webp", redirigir={"stock": "/paginas/"})
        a = next(x for x in S.catalogo()["apps"] if x["id"] == "spsil")
        self.assertEqual((a["tema"], a["logo"], a["redirigir"]), ("spsil", "spsil-logo.webp", {"stock": "/paginas/"}))

    def test_cloudflare_crea_el_registro_y_no_pisa_otro(self):
        llamadas = []

        def cf(metodo, ruta, datos=None, token=""):
            llamadas.append((metodo, ruta))
            if ruta.startswith("/zones?name=ejemplo.com"):
                return [{"id": "z1"}]
            if ruta.startswith("/zones?"):
                return []
            if "dns_records?name=nuevo" in ruta:
                return []
            if "dns_records?name=viejo" in ruta:
                return [{"type": "A", "content": "9.9.9.9"}]
            return {}
        with mock.patch.object(S, "_cf", side_effect=cf):
            S.cloudflare_registro("nuevo.ejemplo.com", "1.2.3.4")
            S.cloudflare_registro("viejo.ejemplo.com", "1.2.3.4")
        posts = [x for x in llamadas if x[0] == "POST"]
        self.assertEqual(posts, [("POST", "/zones/z1/dns_records")])


if __name__ == "__main__":
    unittest.main()


class ComprobarDominio(unittest.TestCase):
    """«Responde» mira el panel en el dominio de una app principal y la página
    de versión de la app en el de una app suelta (ahí todo va a la app)."""

    def _mirada(self, manifiesto):
        pedidas = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def abrir(req, timeout=0):
            pedidas.append(req.full_url)
            return Resp()

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(S, "DIR", Path(d)), mock.patch.object(S, "cfg", return_value={}), \
                mock.patch.object(S, "ip_servidor", return_value=""), \
                mock.patch.object(S, "registro", return_value={"x": {"dominio": "x.ejemplo.com", "manifiesto": manifiesto}}), \
                mock.patch.object(S.socket, "gethostbyname", return_value="1.2.3.4"), \
                mock.patch.object(S, "npm_host", return_value={"forward_host": "entrada-pro", "certificate_id": 1}), \
                mock.patch.object(S.urllib.request, "urlopen", abrir):
            estado = S.comprobar_dominio("x")
        return pedidas, estado

    def test_app_suelta_mira_su_version(self):
        pedidas, estado = self._mirada({"ruta": "soporte", "version": "version/"})
        self.assertEqual(pedidas, ["https://x.ejemplo.com/version/"])
        self.assertTrue(estado["ok"])

    def test_app_principal_mira_el_panel(self):
        pedidas, _ = self._mirada({"ruta": "spsil", "principal": True, "version": "version/"})
        self.assertEqual(pedidas, ["https://x.ejemplo.com/ajustes/salud"])
