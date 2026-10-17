# AGENTS.md

Para una IA que vaya a añadir un producto o una app al servidor DEV/PRO, o a
cambiar este repositorio.

**Lee primero [docs/README.md](docs/README.md)** (cómo está montado y las reglas),
luego [docs/NUEVA_APP.md](docs/NUEVA_APP.md) o
[docs/NUEVO_PRODUCTO.md](docs/NUEVO_PRODUCTO.md), y el [README.md](README.md) para
el manifiesto `servidor.json` campo a campo. Las reglas de código de este
repositorio están en [CLAUDE.md](CLAUDE.md).

Lo que no se hace:

- Meter código de una app aquí, o código del servidor en una app. Una app se
  enchufa solo con su `servidor.json`.
- Escribir a mano un dominio, una IP, una persona o un nombre de app en el código.
- Desplegar, cambiar dominios o tocar Keycloak con órdenes a mano en el servidor
  cuando hay un botón en Ajustes. Si algo de verdad necesita una orden a mano,
  dilo y apúntalo en `docs/` como lo que todavía no hace el servidor.
- Pasar algo a PRO que no funcione en DEV, o pulsar «Poner» en un entorno cuya app
  apunta a un contenedor de antes con datos reales.
- Pedir que se peguen secretos (tokens, claves, client secrets) en un chat: se
  guardan en Ajustes, en los secretos de GitHub o en el servidor.

Para una app nueva, prefiere `despliegue.tipo: "servidor"` (sin script ni secretos
de SSH); usa `workflow` copiando `larceing/APP_TAREAS_SPSIL` solo si la app necesita
su propio script en el servidor.

Si cambias cómo funciona el servidor, cambia también `docs/` en el mismo PR.
