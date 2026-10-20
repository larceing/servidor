# Servidor DEV/PRO

La parte común del servidor, separada de las apps: la **entrada** de cada entorno
(`dev.<dominio>` y `pro.<dominio>`, que reparte por ruta: `/tareas/`, `/linai/`…),
la **portada** (`/`, con login y solo las apps de cada uno), **Ajustes**
(`/ajustes/`, solo IT: versiones de todas las apps en DEV y PRO, «Pasar a PRO»,
dominios propios, grupos) y lo que hay que hacer en **Keycloak** para cada app.

No sabe nada del código de ninguna app. Cada app vive en su repositorio, con su
propio despliegue, y se da de alta aquí con un **manifiesto** (`servidor.json`).

```
navegador ─► Nginx Proxy Manager ─► entrada-dev / entrada-pro (nginx, lo escribe servidor.py)
                                         ├─ /           ─► panel (portada)
                                         ├─ /ajustes/   ─► panel (Ajustes, login, salud)
                                         └─ /<ruta>/    ─► el contenedor de cada app
panel ── peticiones/ ──► agente (servidor.py agente, systemd, root)
      ◄─ estado/     ──    entrada, Keycloak, Nginx Proxy Manager, apps "servidor"
panel ──► GitHub API: lanzar el workflow de cada app, aprobar lo que espera para PRO
```

- `servidor.py`: el programa del servidor (Python 3, sin dependencias). Corre como
  servicio (`servidor-agente`) y como orden (`servidor estado`, `servidor soltar dev`…).
- `panel/`: la portada y Ajustes (FastAPI). El propio servidor construye su imagen.
- `pruebas/` y `panel/tests/`: pruebas; GitHub las pasa en cada cambio.

## Instalar (una vez)

En el servidor, como root:

```bash
curl -fsSL https://raw.githubusercontent.com/larceing/servidor/main/servidor.py | python3 - instalar dev
```

Baja este repositorio a `/opt/apps/servidor/codigo`, saca la configuración de la
instalación que ya hubiera (dominios, Keycloak, red del proxy, el token de GitHub y
las apps del Ajustes de antes), crea el cliente `servidor` en Keycloak, arranca el
panel, toma la entrada de DEV y deja el servicio que lo mantiene. Para PRO:
`servidor tomar pro`. Marcha atrás: `servidor soltar dev` (vuelve la entrada de antes).

Después no hace falta la terminal: cada cambio que llega a `main` con las pruebas en
verde se instala solo (el agente lo mira cada minuto; si el panel nuevo no arranca,
se queda con el de antes y lo apunta en `historial.log`).

## Dar de alta una app: `servidor.json`

En la raíz de su repositorio. Ajustes → *Añadir una aplicación* → `usuario/repo`.

```json
{
  "nombre": "Gestor de Tareas",
  "ruta": "tareas",
  "grupo": "Tareas",
  "contenedor": "tareas-{entorno}",
  "puerto": 8000,
  "prefijo": "conservar",
  "version": "version/",
  "despliegue": {
    "workflow": "despliegue.yml",
    "rama": "main",
    "campo_entorno": "entorno",
    "campo_version": "version",
    "anterior": true
  }
}
```

| Campo | Qué es | Por defecto |
|---|---|---|
| `nombre` | lo que se ve en la portada | la ruta |
| `ruta` | `/ruta/` en DEV y PRO | (obligatorio) |
| `grupo` | grupo de Keycloak sin `/DEV/` ni `/PRO/`: la ven `/DEV/<grupo>` en DEV y `/PRO/<grupo>` en PRO, y sus subgrupos. Se crean solos, con `/Admin` | solo IT |
| `contenedor` | nombre del contenedor en la red del proxy (`{entorno}` = dev o pro) | `{ruta}-{entorno}` |
| `puerto` | donde escucha | 8000 |
| `prefijo` | `quitar`: la app recibe `/...`; `conservar`: recibe `/ruta/...`. En los dos casos llega `X-Forwarded-Prefix: /ruta` | `quitar` |
| `entrada` | `servidor`: la ruta la pone el servidor; `propia`: la app escribe aún su `.conf` (transición) | `servidor` |
| `version` | camino (dentro de su ruta) que responde 200 con `{"version": "..."}` | `version/` |
| `icono` | fichero de la app (dentro de su ruta, sin pedir sesión) que sale en su tarjeta de la portada, p. ej. `img/logo.png`. Sin él, la inicial | — |
| `despliegue.workflow` | su workflow con `workflow_dispatch` y dos campos: entorno (dev/pro) y versión | — |
| `despliegue.anterior` | su workflow entiende la versión `anterior` (volver atrás) | `false` |
| `despliegue.tipo` | `workflow`, `servidor` (el servidor la despliega desde `imagen`) o `ninguno` | `workflow` |
| `despliegue.imagen` | (tipo `servidor`) imagen sin versión, p. ej. `ghcr.io/usuario/app` | — |
| `keycloak.cliente` | su cliente de Keycloak, p. ej. `{ruta}-{entorno}`: se crea si falta y su secret queda en `/opt/apps/servidor/secretos/<cliente>.env` | (tipo `servidor`: `{ruta}-{entorno}`) |
| `dentro` | la ruta de su app principal (ver abajo): va en `/<principal>/<ruta>/`. También se elige al añadirla | — |

Varias apps en un repositorio: `{"apps": [ {...}, {...} ]}`.

### Lo que el servidor espera de una app

- Que su contenedor esté en la red del proxy con el nombre de `contenedor`.
- Que responda su `version` (para el punto verde, la versión de cada entorno y «Pasar a PRO»).
- Que construya sus direcciones con `X-Forwarded-Prefix` y el `Host` de la petición: así
  vale igual en `pro.<dominio>/ruta/` que en un dominio propio.
- Su login: Keycloak, grupo `<ENTORNO>/<grupo>` para entrar, `IT` administra todo.

### Apps principales (varias apps juntas, p. ej. las de un cliente)

Ajustes → **Nueva app principal** (p. ej. `spsil`, «SPSIL»). Es una portada que agrupa
subapps; no se despliega. Cada subapp se añade como cualquier app eligiendo **Dentro de**,
y se despliega por su cuenta, por DEV y PRO como todas:

| | DEV | PRO | PRO en su dominio |
|---|---|---|---|
| Portada de la principal | `dev…/spsil/` | `pro…/spsil/` | `spsil.dominio/` |
| Una subapp | `dev…/spsil/tareas/` | `pro…/spsil/tareas/` | `spsil.dominio/tareas/` |
| La ven | `/DEV/SPSIL/Tareas` | `/PRO/SPSIL/Tareas` | `/PRO/SPSIL/Tareas` |

Los grupos son iguales en todo el servidor (`/<ENTORNO>/<Principal>/<Grupo>`, con `/Admin`).
Su contenedor por defecto es `<principal>-<ruta>-<entorno>`; si en un entorno ya existe con
otro nombre, se dice al añadirla (*Si ya existe con otro nombre*). Cuando PRO va a su
dominio, `pro…/spsil/…` lleva allí, la portada de ese dominio tiene su propio cliente de
Keycloak (`servidor-<principal>`, con el tema que se le ponga) y su logo, y se pueden
redirigir rutas viejas (`stock>/paginas/`; a otro dominio sin camino, p. ej. `auth>https://users.midominio.com`, se lleva el camino entero).

### Pasar a PRO

Ajustes → **Pasar a PRO**: pone en PRO la versión que DEV dice tener. Con `workflow`,
lanza su workflow con `entorno=pro` y esa versión, y si GitHub pide aprobación (entorno
`pro` con revisores) la aprueba sola. Con `servidor`, la despliega el agente, con
comprobación de salud y vuelta a la anterior si no responde.

### Desplegar sin el navegador (API)

Ajustes → **Acceso por API** crea una clave (por defecto solo DEV) para que un
programa, p. ej. Claude, haga lo mismo que «Poner» y vea cómo va:
`POST /ajustes/api/v1/apps/<id>/desplegar`. Todo queda apuntado en Ajustes. Ver
[docs/API.md](docs/API.md).

### PRO en su dominio

Ajustes → la app (o la principal) → *Más* → **Poner en su dominio** (solo PRO). El
servidor, por orden:

1. **DNS**: con el token de Cloudflare guardado (Ajustes → *Cloudflare*, permiso
   «Zone → DNS → Edit»), crea el registro `A` hacia el servidor sin proxy (nube gris).
   Si ya hay uno que va a otro sitio, no lo toca y lo dice. Sin token, el registro lo
   pones tú.
2. **Entrada de PRO**: sirve ese dominio.
3. **Nginx Proxy Manager**: el host con su certificado hacia la entrada de PRO. Si el
   dominio ya iba a otro sitio (p. ej. un portal de antes), lo apunta para poder volver,
   y copia de allí el logo, `/marca/` y `/js/`.
4. **Keycloak**: añade la dirección a los clientes de la app (y de sus subapps).

**⟳ Sincronizar** repite todo eso y arregla lo que falte; cada 10 minutos el servidor
mira solo si el DNS, el proxy, el certificado y la respuesta siguen bien, y Ajustes lo
enseña. **Quitar su dominio** lo devuelve a donde iba antes (el DNS no se toca). La app
tiene que aceptar esa dirección (ver *Lo que el servidor espera de una app*).

## Configuración: `/opt/apps/servidor/servidor.env`

La escribe `instalar`. `DOMINIO_DEV`, `DOMINIO_PRO`, `KEYCLOAK_ISSUER`,
`KEYCLOAK_CLIENTE`/`KEYCLOAK_SECRETO` (cliente de la portada), `GRUPO_IT`, `RED_PROXY`,
`ENTORNOS_GESTIONADOS`, `AJUSTES_EN` (dónde está Ajustes: PRO), `LEGADO` (la entrada de
antes, cuyas rutas se siguen copiando hasta que cada app tenga su manifiesto),
`PANEL_CLAVE`, `SERVIDOR_IP` (opcional: la IP para el DNS; si no, la de `DOMINIO_PRO`).
Aparte, solo para root: `.npm.env` (acceso a Nginx Proxy Manager) y `.cloudflare.env`
(token de Cloudflare). Ningún dominio, servidor ni persona va en este repositorio.

## Pruebas

```bash
pip install -r panel/requirements.txt
python -m unittest discover -s pruebas -v
(cd panel && python -m unittest discover -s tests -v)
```
