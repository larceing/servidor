# Guía del servidor: cómo está montado y cómo se añade algo

Esta carpeta es para quien tenga que **añadir un producto o una app nueva** al
servidor DEV/PRO, sea una persona o una IA. Se lee en este orden:

| Documento | Para qué |
|---|---|
| Este fichero | Cómo está montado todo y las reglas que no se rompen |
| [NUEVA_APP.md](NUEVA_APP.md) | Paso a paso: una app nueva (suelta o dentro de un producto) |
| [NUEVO_PRODUCTO.md](NUEVO_PRODUCTO.md) | Paso a paso: un producto nuevo (p. ej. un cliente) con su portada, sus apps y su dominio |
| [../README.md](../README.md) | La referencia: el manifiesto `servidor.json` campo a campo, la configuración y las pruebas |
| [API.md](API.md) | La API de Ajustes: desplegar con una clave (p. ej. Claude), por defecto solo en DEV |

Si eres una IA, lee también [../AGENTS.md](../AGENTS.md).

## Las reglas

1. **Todo aislado.** Este repositorio no tiene código de ninguna app, y ninguna app
   tiene código del servidor. Una app se enchufa con un fichero (`servidor.json` en
   su repositorio) y no toca a las demás.
2. **Todo automático.** Se añade, se despliega y se pone en su dominio desde
   **Ajustes** (`/ajustes/` de PRO), con botones. Si para algo hace falta una orden a
   mano en el servidor, es una excepción que hay que apuntar y, si se puede, quitar.
3. **Nada fijo en el código.** Ni dominios, ni IPs, ni personas, ni nombres de apps:
   salen de `servidor.env` (en el servidor) o del manifiesto de cada app. En la
   documentación se escribe `<dominio>`.
4. **DEV antes que PRO.** Todo pasa por DEV; PRO solo recibe una versión que ya
   funciona en DEV («Pasar a PRO»). Todo lo que se hace se puede deshacer
   («↩ Anterior», «Quitar su dominio», «Quitar del panel»).

## El dibujo

```
navegador
   │
   ▼
Nginx Proxy Manager  (certificados; un host por dominio)
   │
   ├─ dev.<dominio> ─► entrada-dev ─┐
   ├─ pro.<dominio> ─► entrada-pro ─┤   (nginx; los escribe servidor.py)
   └─ <producto>.<dominio> ─► entrada-pro (cuando un producto va a su dominio)
                                    │
                                    ├─ /             ─► panel: la portada
                                    ├─ /ajustes/     ─► panel: Ajustes (solo IT)
                                    ├─ /<app>/       ─► contenedor de esa app
                                    ├─ /<producto>/  ─► portada del producto
                                    └─ /<producto>/<app>/ ─► contenedor de esa subapp

panel ── peticiones/ ──► agente (servidor.py agente, systemd, root)
      ◄── estado/   ──     entradas, Keycloak, Nginx Proxy Manager, Cloudflare,
                           apps de tipo "servidor"
panel ──► GitHub API:      lanzar el workflow de cada app, aprobar lo que espera para PRO
programa (Claude) ──► /ajustes/api/v1/ con una clave ──► lo mismo que «Poner» (ver API.md)
```

- **Entrada** (`entrada-dev`, `entrada-pro`): un nginx por entorno que reparte por
  ruta. Nadie la escribe a mano: `servidor.py` la genera desde el registro de apps,
  la prueba con `nginx -t` y, si falla, se queda con la anterior.
- **Panel** (`panel/`, contenedor `servidor-panel`): la portada (`/`, cada persona ve
  solo las apps de sus grupos) y Ajustes (`/ajustes/`, solo el grupo IT). No toca el
  servidor: deja peticiones en `peticiones/` y lee lo que el agente escribe en `estado/`.
- **Agente** (`servidor.py agente`, servicio `servidor-agente`): atiende las
  peticiones del panel (dar de alta, grupos, dominios, desplegar apps de tipo
  `servidor`), rehace las entradas y **se actualiza solo** cuando llega a `main` un
  commit con las pruebas de GitHub en verde.
- **Keycloak**: un solo realm (`general`) para todo. Los grupos y, si hace falta, los
  clientes de cada app los crea el servidor al darla de alta.
- **Nginx Proxy Manager** y **Cloudflare**: los usa el servidor para «Poner en su
  dominio» (certificado, host y registro DNS). Sus accesos se guardan en Ajustes.

Todo vive en el servidor en `/opt/apps/servidor/` (ver el docstring de
`servidor.py`): `servidor.env`, `registro/` (un JSON por app dada de alta),
`entrada/<ENT>/`, `estado/`, `peticiones/`, `apps/<id>/<ENT>/`, `secretos/`.

## Nombres: rutas, grupos y contenedores

Todo sale de tres datos: la **ruta** de la app, su **grupo** y, si va dentro de un
producto, la ruta y el grupo de ese producto.

| | App suelta (`tareas`, grupo `Tareas`) | App dentro de un producto (`spsil` → `tareas`) |
|---|---|---|
| Dirección en DEV | `dev.<dominio>/tareas/` | `dev.<dominio>/spsil/tareas/` |
| Dirección en PRO | `pro.<dominio>/tareas/` | `pro.<dominio>/spsil/tareas/` |
| PRO en su dominio | (la app sigue también en `pro…/tareas/`) | `spsil.<dominio>/tareas/` |
| Quién entra (DEV) | `/DEV/Tareas` y subgrupos | `/DEV/SPSIL/Tareas` y subgrupos |
| Quién entra (PRO) | `/PRO/Tareas` | `/PRO/SPSIL/Tareas` |
| Quién la administra | `…/Tareas/Admin` | `…/SPSIL/Tareas/Admin` |
| Contenedor por defecto | `tareas-dev`, `tareas-pro` | `spsil-tareas-dev`, `spsil-tareas-pro` |
| Id en el registro | `tareas` | `spsil.tareas` |

- **`/IT`** entra y administra en todas las apps y en Ajustes. A alguien de IT no hace
  falta meterlo en ningún otro grupo.
- Los grupos `/DEV/…` y `/PRO/…` (con su `/Admin`) **se crean solos** al dar de alta la
  app. Lo único manual en Keycloak es meter a cada persona en su grupo.
- Rutas reservadas: `ajustes`, `auth`, `portada`, `version`, `no-disponible`,
  `static`, `salud`.

## Dos formas de desplegar una app

Lo dice `despliegue.tipo` en su manifiesto:

| | `servidor` (recomendado para apps nuevas) | `workflow` (como las apps de Tareas) |
|---|---|---|
| Quién arranca el contenedor | el agente del servidor, con un compose que escribe él | un script de la app en el servidor, llamado por su workflow por SSH |
| Qué pone la app | una imagen (`ghcr.io/<usuario>/<app>`) | workflow con `workflow_dispatch`, script de servidor, compose |
| Pasos a mano en el servidor | ninguno (salvo que la imagen sea privada: ver NUEVA_APP) | uno: el `preparar` de su script (crea la clave SSH y los secretos) |
| Secretos en GitHub | ninguno | `DEPLOY_HOST`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_SSH_KEY` |
| Login | el servidor crea el cliente de Keycloak y le pasa `KEYCLOAK_*`, `APP_URL_PUBLICA`, `APP_RUTA_BASE` | lo configura su script (`keycloak-crear`) |
| Datos | un volumen `/datos` | lo que diga su compose (p. ej. su base en la MariaDB compartida) |
| Comprobación y vuelta atrás | sí (si no responde, vuelve a la de antes) | lo que haga su script (los de Tareas, sí) |

Aris (el Gestor de Tareas propio, en `/tareas/`) y Tareas SPSIL se despliegan a
propósito por su workflow y `tareas.sh` / `spsil-tareas.sh`: usan la MariaDB
compartida, hacen copia antes de desplegar, vuelven atrás solos y saben importar
una instalación de antes. Son una excepción, **no el modelo para apps nuevas**.
Cada una entra al servidor por SSH con su propia clave (p. ej. `github-despliegue-aris`),
limitada a ejecutar `<script> ci`; el SSH del servidor solo acepta claves, no contraseñas.

Hay un tercer valor, `ninguno`, para una app que va en el contenedor de otra (p. ej.
Páginas, que sirve el contenedor de Lin-AI): sale en la portada pero no se despliega.

## Lo que hay hoy

| App | Dónde | Repositorio | Despliegue |
|---|---|---|---|
| Aris (gestor de tareas propio) | `/tareas/` | `larceing/aris` | `workflow` (`despliegue.yml` + `tareas.sh`) |
| SPSIL (producto) | `/spsil/`; en PRO, en su dominio `spsil.<dominio>` | — (se crea en Ajustes: «Nueva app principal») | no se despliega |
| Gestor de Tareas de SPSIL | `/spsil/tareas/` | `larceing/APP_TAREAS_SPSIL` | `workflow` (`despliegue.yml` + `spsil-tareas.sh`) |
| Lin-AI | `/spsil/linai/` | `larceing/spsil-proyecto` | `workflow` (`linai.yml` + `lin-ai/despliegue/linai.sh`) |
| Páginas | `/spsil/paginas/` | `larceing/spsil-proyecto` (mismo `servidor.json`) | `ninguno` (contenedor de Lin-AI) |

Las apps de SPSIL usan los grupos `/DEV/SPSIL/…` y `/PRO/SPSIL/…`; los grupos sueltos
de antes en la raíz de Keycloak ya no existen. Todas las rutas, también `/tareas/`,
las escribe el servidor.

`larceing/APP_TAREAS_BASE` es solo el **producto base** del gestor de tareas: de ahí
salen Aris y Tareas SPSIL (con `git merge`), pero no se despliega en este servidor.

### Lo que ya no existe

Antes, Tareas hacía de servidor: tenía su propio Ajustes y su portada, un workflow
`apps.yml` para crear apps y las órdenes `app-nueva`, `app-desplegar`, `portada` y
`dominio` de `tareas.sh`. Todo eso se quitó: lo hace este servidor. Si un documento
viejo (p. ej. `docs/MANUAL_VERSIONES.md` o `docs/DESPLIEGUE.md` de una app, o notas
sueltas) lo menciona, está desactualizado; manda esta guía. La app de stock antigua de
SPSIL también se quitó (su ruta vieja `/stock` lleva a Páginas).

## Cómo se cambia este repositorio

Por PR a `main`. GitHub pasa las pruebas (`.github/workflows/pruebas.yml`); al
fusionar, el agente del servidor lo instala solo en cuanto el commit está en verde,
y si el panel nuevo no arranca se queda con el anterior (lo apunta en
`historial.log`). No hace falta entrar al servidor.
