# Añadir una app nueva

Una app nueva es un repositorio propio que se enchufa al servidor con un
`servidor.json`. Al terminar tendrá su tarjeta en Ajustes, su ruta en DEV y PRO, sus
grupos de Keycloak y sus botones «Poner», «↩ Anterior» y «Pasar a PRO».

Antes de empezar, decide dos cosas:

1. **¿Suelta o dentro de un producto?** Suelta: `/<ruta>/`. Dentro de un producto
   (p. ej. de SPSIL): `/<producto>/<ruta>/`, y el producto tiene que existir antes
   (ver [NUEVO_PRODUCTO.md](NUEVO_PRODUCTO.md)).
2. **¿Cómo se despliega?** Para una app nueva, **tipo `servidor`** (receta A): no
   necesita nada en el servidor ni secretos de SSH. El tipo `workflow` (receta B) es
   el de las apps de Tareas, para cuando la app necesita su propio script en el
   servidor (base de datos compartida, copias, importar datos de una instalación
   vieja…).

## 1. Lo que tiene que cumplir la app (las dos recetas)

- **Un contenedor** que escuche en un puerto (por defecto 8000) y no publique puertos:
  el servidor llega a él por la red del proxy, por su nombre.
- **Una página de versión**: `GET <su ruta>/version/` responde 200 con
  `{"version": "sha-1a2b3c4"}` (la etiqueta de la imagen que corre). Con ella Ajustes
  pinta el punto verde, enseña la versión de cada entorno y sabe qué pasar a PRO.
- **Vivir bajo una ruta.** La entrada le manda `X-Forwarded-Prefix: /<ruta>` (o
  `/<producto>/<ruta>`). Con `"prefijo": "quitar"` la app recibe `/...`; con
  `"conservar"` recibe el camino entero. Las direcciones que la app escribe (enlaces,
  redirecciones, cookies) tienen que salir de ese prefijo y del `Host` de la
  petición, nunca fijas: así sirve igual en `pro.<dominio>/x/` que en un dominio
  propio. (Ejemplo hecho: `ruta_base.py` en APP_TAREAS_BASE.)
- **Login con Keycloak** (realm `general`, flujo Authorization Code): entra quien esté
  en el grupo de su entorno (p. ej. `/DEV/SPSIL/Tareas` o subgrupos) y quien esté en
  `/IT`; administra `…/Admin` e `/IT`.
- **Imagen en GitHub Container Registry** con etiqueta por versión: `sha-xxxxxxx` en
  cada push a `main`, `vX.Y.Z` en cada release.

## 2. Receta A: el servidor despliega la imagen (tipo `servidor`)

### En el repositorio de la app

`servidor.json` en la raíz:

```json
{
  "nombre": "Inventario",
  "ruta": "inventario",
  "grupo": "Inventario",
  "puerto": 8000,
  "prefijo": "quitar",
  "version": "version/",
  "despliegue": {
    "tipo": "servidor",
    "imagen": "ghcr.io/<usuario>/inventario",
    "memoria_mb": 512
  }
}
```

- Si va dentro de un producto, añade `"dentro": "<ruta del producto>"` (o elígelo en
  Ajustes al añadirla).
- El cliente de Keycloak se crea solo (`<ruta>-<entorno>`, o
  `<producto>-<ruta>-<entorno>`). Para otro nombre: `"keycloak": {"cliente": "…"}`.

Un workflow que construya y suba la imagen en cada push a `main` y en cada release
(basta el job `imagen` de `.github/workflows/despliegue.yml` de APP_TAREAS_SPSIL).
No hace falta job de despliegue ni secretos.

### Lo que el servidor le da a la app

Al desplegar, el agente escribe `/opt/apps/servidor/apps/<id>/<ENT>/` con un compose
(red del proxy, volumen `datos` en `/datos`, límite de memoria) y un `app.env` con:

| Variable | Valor |
|---|---|
| `SERVIDOR_ENTORNO` | `dev` o `pro` |
| `APP_URL_PUBLICA` | `https://<host>/<camino>` (cambia sola cuando va a su dominio) |
| `APP_RUTA_BASE` | `/<ruta>` o `/<producto>/<ruta>` |
| `KEYCLOAK_ISSUER` | el del servidor |
| `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET`, `AUTH_MODO=sso` | su cliente |
| `KEYCLOAK_GRUPO_APP` | `DEV/<Grupo>` o `DEV/<Producto>/<Grupo>` (y `PRO/…`) |
| `KEYCLOAK_GRUPO_ADMIN_GLOBAL` | el grupo de IT |

Si la app necesita **otros secretos** (una clave de API, etc.), hoy van en ese
`app.env` a mano: el servidor conserva lo que no es suyo. No hay aún un sitio en
Ajustes para ellos.

**Imagen privada:** el servidor tiene que poder descargarla. Si el paquete de GHCR es
privado, hace falta una vez `docker login ghcr.io` en el servidor con un token de
lectura de paquetes (sirve para todas las apps de ese usuario). Si no, «Poner» dice
«no se puede descargar…».

### En Ajustes

1. Si el token de GitHub de Ajustes es de los que llevan lista de repositorios,
   añade el repositorio nuevo a esa lista (permisos: Actions, Contents y Deployments,
   lectura y escritura).
2. **Añadir una aplicación**: repositorio `<usuario>/<repo>`, rama `main` y, si va en un
   producto, «Dentro de». **Añadir.** Se crean los grupos y el cliente de Keycloak, y
   la ruta en las entradas.
3. En la tarjeta, fila **DEV**: escribe la versión (`sha-xxxxxxx`, la del último
   push) y **Poner**. El agente la descarga, la arranca, espera a que responda su
   `version/` y, si no responde, vuelve a la de antes.
4. Prueba en `dev.<dominio>/<ruta>/`.
5. **Pasar a PRO** cuando esté bien.

## 3. Receta B: la app se despliega con su workflow (tipo `workflow`)

Es lo que hacen Aris, Tareas SPSIL y Lin-AI, a propósito (base de datos compartida,
copias antes de desplegar, importar una instalación de antes). No es el modelo para
apps nuevas: úsala solo si la app necesita lo mismo. La plantilla más limpia es
**APP_TAREAS_SPSIL**; cópiala y cambia los nombres:

| Fichero | Qué hace |
|---|---|
| `servidor.json` | ruta, grupo, contenedor y `despliegue.workflow` |
| `.github/workflows/despliegue.yml` | pruebas → imagen → DEV en cada push a `main`; con «Run workflow» (que es lo que lanza Ajustes) pone una versión en `dev`/`pro`, o `anterior` |
| `.github/actions/desplegar/action.yml` | entra por SSH al servidor con una clave que solo puede ejecutar `<script> ci` |
| `despliegue/servidor/<app>.sh` | en el servidor: `preparar`, `desplegar`, `rollback`, `keycloak-crear`, `estado`, `copia` |
| `despliegue/servidor/docker-compose.yml` | el compose de cada versión (va dentro de la imagen; el script lo saca de ella) |

```json
{
  "nombre": "Gestor de Tareas",
  "ruta": "tareas",
  "dentro": "spsil",
  "grupo": "Tareas",
  "contenedor": "spsil-tareas-{entorno}",
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

Pasos, por orden:

1. **En el servidor, una vez** (es el paso a mano de esta receta): descargar el script
   y `preparar`. Crea las carpetas y secretos de cada entorno y una clave SSH limitada,
   y dice qué poner en GitHub. Luego `keycloak-crear dev` (y `pro` cuando toque).
   Ejemplo real: `servidor/spsil-en-dev.md` en los ficheros del proyecto.
2. **En GitHub** (Settings → Secrets and variables → Actions), los tres secretos:
   `DEPLOY_HOST` (IP del servidor), `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan -t ed25519
   <IP>`), `DEPLOY_SSH_KEY` (la clave privada que creó `preparar`; una por app, p. ej.
   `github-despliegue-<app>`, que en el servidor solo puede ejecutar `<script> ci`). Sin
   ellos el workflow avisa y no despliega. El SSH del servidor solo acepta claves, nunca
   contraseñas, y la clave no se pega en ningún chat. Opcional: un entorno `pro` con revisores; Ajustes
   aprueba solo lo que lanza con «Pasar a PRO».
3. **En Ajustes**: igual que la receta A (token, **Añadir una aplicación**, **Poner**
   en DEV, **Pasar a PRO**). Aquí «Poner» lanza el workflow con `entorno` y `version`.

## 4. Comprobar

- La tarjeta tiene el punto verde en DEV y enseña la versión.
- `dev.<dominio>/` (la portada) enseña la app a quien está en su grupo, y a IT.
- La app abre en su ruta, el login vuelve a la app y los enlaces no pierden la ruta.

## 5. Si algo falla

| Síntoma | Mira |
|---|---|
| «GitHub no encuentra eso» al añadir | el repositorio y la rama, que haya `servidor.json` en la raíz y que el token vea ese repo |
| Punto rojo / «no disponible» | que el contenedor esté en la red del proxy con el nombre de `contenedor`, el `puerto` y que `version/` responda 200 |
| La app abre pero los enlaces van a `/` | la app no usa `X-Forwarded-Prefix` / `APP_RUTA_BASE` |
| El login da «redirect_uri no válido» | la dirección no está en el cliente de Keycloak: «Guardar grupos» en la tarjeta (rehace grupos y cliente de tipo `servidor`), «⟳ Sincronizar» si está en su dominio, o `keycloak-crear` del script (tipo `workflow`) |
| «Pasar a PRO» dice que no sabe la versión | la página `version/` de DEV no responde o no da `{"version": …}` |
| El resultado de una acción | Ajustes enseña la salida de cada petición; en el servidor, `estado/resultados/` |

## 6. Para quitarla

Ajustes → la app → *Más* → **Quitar del panel**. Quita la ruta y la tarjeta; sus
contenedores, datos y grupos de Keycloak se quedan como estaban.
