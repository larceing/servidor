# La API de Ajustes (para Claude y otros programas)

Lo mismo que los botones «Poner» y «Pasar a PRO» de Ajustes, pero con una **clave**
en vez de la sesión de alguien en el navegador. Sirve para que una IA (p. ej. un
hilo de Claude) despliegue sola en DEV y vea cómo va, sin que nadie pulse nada.

```
https://pro.<dominio>/ajustes/api/v1/
```

## Las claves

Se crean en **Ajustes → Acceso por API → Nueva clave** (solo IT):

- **Nombre**: quién la usa (p. ej. `Claude`). Sale en el registro y en «pedido por api:Claude».
- **Entornos**: por defecto **solo DEV**. PRO hay que marcarlo a propósito
  («También PRO»); sin eso, la clave recibe `403` en cualquier cosa de PRO y
  «Pasar a PRO» lo sigue pulsando una persona en Ajustes.
- **Solo estas apps**: vacío = todas; si no, sus ids (`sindri`, `spsil.tareas`…).
- **Caduca**: 30 días, 90 (por defecto), un año o nunca.

La clave (`srv_<id>_<secreto>`) se enseña **una sola vez**, al crearla. En el
servidor solo queda su sha256 (en el volumen `/datos` del panel, `api.json`).
Se quita con «Quitar» y deja de funcionar al momento.

**Nunca se pega en un chat.** Para Claude Code en la nube va como secreto del
entorno: Ajustes del proyecto → *Cloud environment* → el entorno → variable
`SERVIDOR_TOKEN`. Ese entorno, además, tiene que dejar salir a `pro.<dominio>`
(*Network access* → *Allowed domains*).

Todo lo que se pide con una clave (y los intentos con una clave que no vale)
queda en el registro de Ajustes (`/datos/api.log`), y cada petición al agente
lleva `"quien": "api:<nombre>"`.

## Las llamadas

Todas con `Authorization: Bearer <clave>`. La sesión del navegador **no** vale
aquí (así no hace falta CSRF y una pestaña abierta no puede usar la API).

| Método y camino | Qué hace |
|---|---|
| `GET /yo` | Lo que puede hacer la clave (entornos, apps, caducidad) |
| `GET /apps` | Las apps que ve la clave: versión en DEV y PRO, si responden, si puede desplegar en cada uno |
| `GET /apps/<id>` | Una app: lo que hay en marcha (`en_curso`), sus últimas ejecuciones de GitHub y lo último del agente |
| `POST /apps/<id>/desplegar` | `{"entorno": "dev", "version": "sha-1234567"}` (o `"anterior"`) |
| `POST /apps/<id>/pasar-a-pro` | Lo que tiene DEV, a PRO (solo con una clave con PRO) |
| `GET /estado` | Si el agente del servidor está vivo, lo que hay en cola y lo último que ha hecho |
| `GET /traspaso` | **Las instrucciones para quien vaya a usar la clave** (Markdown), con las apps y versiones de ahora mismo |

Respuestas: `202` pedido · `400` datos no válidos · `401` clave que falta, no
vale o ha caducado · `403` la clave no puede en ese entorno · `404` app que no
existe o que la clave no ve · `409` ya hay un despliegue de esa app en marcha (de
quien sea: botón o API) o no se sabe la versión de DEV · `502` GitHub no lo ha
aceptado.

Un despliegue sigue las mismas reglas que el botón: uno por app a la vez; las de
tipo `servidor` las pone el agente con comprobación de salud y vuelta a la
anterior; las de tipo `workflow` lanzan su workflow en GitHub.

## Cómo lo usa un hilo de Claude

```bash
export SERVIDOR_API="https://pro.<dominio>/ajustes/api/v1"
H="Authorization: Bearer $SERVIDOR_TOKEN"

curl -fsS -H "$H" "$SERVIDOR_API/traspaso"     # 1. leer qué puede hacer y qué hay
curl -fsS -H "$H" "$SERVIDOR_API/apps/sindri"  # 2. versión actual en DEV

# 3. pedir la versión nueva en DEV (la etiqueta de la imagen que ha publicado su CI)
curl -sS -H "$H" -H 'Content-Type: application/json' \
     -d '{"entorno": "dev", "version": "sha-1234567"}' "$SERVIDOR_API/apps/sindri/desplegar"

# 4. repetir GET /apps/sindri cada ~15 s hasta que en_curso sea null y
#    entornos.dev.version sea la pedida; si sale en_curso.fallo, ha fallado:
#    volver con {"version": "anterior"} y decirlo.
```

`/traspaso` es el «traspaso» entre hilos: un hilo nuevo no necesita que nadie le
explique cómo se despliega; lo pide y recibe las reglas, las llamadas y la tabla
de apps con sus versiones, sin la clave dentro.
