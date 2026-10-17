# Añadir un producto nuevo

Un **producto** (en Ajustes, «app principal») agrupa varias apps bajo una portada
propia, normalmente las de un cliente. Ejemplo hecho: **SPSIL**, con Gestor de
Tareas, Lin-AI y Páginas. El producto no se despliega: se despliega cada una de sus
apps, por DEV y PRO como todas.

| | DEV | PRO | PRO en su dominio |
|---|---|---|---|
| Portada del producto | `dev.<dominio>/spsil/` | `pro.<dominio>/spsil/` | `spsil.<dominio>/` |
| Una de sus apps | `dev.<dominio>/spsil/tareas/` | `pro.<dominio>/spsil/tareas/` | `spsil.<dominio>/tareas/` |
| Quién la ve | `/DEV/SPSIL/Tareas` | `/PRO/SPSIL/Tareas` | `/PRO/SPSIL/Tareas` |

Cuando PRO va a su dominio, `pro.<dominio>/spsil/…` redirige allí.

## 1. Crear el producto (Ajustes de PRO)

**Nueva app principal**: ruta (`spsil`), nombre (`SPSIL`) y grupo de Keycloak
(`SPSIL`; si se deja vacío, el nombre). **Crear.** Ya sale en las dos portadas para IT.

## 2. Meterle apps

Cada app es un repositorio con su `servidor.json` (ver [NUEVA_APP.md](NUEVA_APP.md)).
Al añadirla, elige **Dentro de: SPSIL**, o pon `"dentro": "spsil"` en su manifiesto.

- Sus grupos se crean solos: `/DEV/SPSIL/<Grupo>`, `/PRO/SPSIL/<Grupo>` y sus `/Admin`.
- Su contenedor por defecto es `<producto>-<ruta>-<entorno>` (`spsil-tareas-dev`).
- Varias apps en un repositorio: `{"apps": [ {...}, {...} ]}` (así lo hace
  `larceing/spsil-proyecto` con Lin-AI y Páginas). Al añadirlo, «Solo la app con
  ruta» deja dar de alta una sola.
- **Si la app ya existía con otro contenedor** (una instalación de antes con datos
  reales), al añadirla abre «Si ya existe con otro nombre» y escribe su contenedor en
  DEV o en PRO. El servidor apunta allí y no crea otro. En ese caso **no pulses
  «Poner» ni «Pasar a PRO»** en ese entorno hasta que la instalación vieja se haya
  pasado a la nueva: crearía un contenedor vacío al lado.

Después, en Keycloak, mete a cada persona (que no sea de IT) en el grupo de cada app.

## 3. Ponerlo en su dominio (solo PRO)

Antes, una vez para todo el servidor: en Ajustes, **Acceso a Nginx Proxy Manager** y,
para que el DNS se haga solo, **Cloudflare (DNS)** (token con permiso «Zone → DNS →
Edit»). Sin token de Cloudflare, el registro `A` hacia el servidor (sin proxy, nube
gris) se crea a mano en el DNS.

En la caja del producto → *Más: su dominio, su portada*:

1. Opcional: **tema del login** (un tema que ya exista en Keycloak), **logo** (un
   fichero de su portada) y **rutas viejas** (`stock>/paginas/` lleva una ruta vieja a
   otra; `auth>https://users.<dominio>` manda la ruta a otro dominio con el camino
   entero). **Guardar.**
2. El dominio (`spsil.<dominio>`) y **Poner en su dominio**. El servidor hace, por
   orden: DNS (si tiene el token), la entrada de PRO, el host de Nginx Proxy Manager
   con certificado y Keycloak (crea el cliente de la portada `servidor-<producto>` y
   añade la dirección a los clientes de sus apps que la declaran en su manifiesto).
3. Si el dominio ya iba a otro sitio (un portal de antes), lo apunta para poder
   volver y copia de allí el logo, `/marca/` y `/js/`.

**⟳ Sincronizar** repite todo y arregla lo que falte; cada 10 minutos el servidor
comprueba DNS, proxy, certificado y respuesta, y Ajustes lo enseña. **Quitar su
dominio** lo devuelve a donde iba antes (el DNS no se toca).

Lo que todavía no hace el servidor:

- **El logo de un producto nuevo** (sin portal anterior del que copiarlo) se sube a
  mano a `/opt/apps/servidor/estado/estaticos/<producto>/` en el servidor.
- **El tema del login** tiene que existir antes en Keycloak.
- **Las apps de tipo `workflow`** que no declaran `keycloak.cliente` tienen que
  aceptar la dirección nueva por su cuenta (su script la añade a su cliente y a su
  `APP_URL_PUBLICA`; p. ej. `linai.sh publicar`).

## 4. Cómo lo hicimos con SPSIL

SPSIL ya existía antes del servidor: un portal propio en `spsil.<dominio>`, su Tareas
con datos reales (contenedor `spsil-tareas`) y su Lin-AI. El camino fue, sin tocar
nunca lo que estaba en uso:

1. **DEV primero** (`servidor/spsil-en-dev.md` en los ficheros del proyecto): crear la
   app principal `spsil`; preparar Tareas SPSIL en el servidor (`spsil-tareas.sh
   preparar` y `keycloak-crear dev`) con sus tres secretos en GitHub; añadir
   `larceing/APP_TAREAS_SPSIL` dentro de SPSIL y **Poner** en DEV; lo mismo con
   `larceing/spsil-proyecto` (Lin-AI y Páginas, tras `linai.sh dentro dev spsil SPSIL`).
2. **PRO apuntando a lo que ya había** (`servidor/spsil-en-pro.md`): añadir cada app
   con «Si ya existe con otro nombre» → su contenedor de PRO de siempre
   (`spsil-tareas`, `spsil-linai`), para no crear otro vacío. Copiar los datos de
   Lin-AI viejo al nuevo con su script (`linai.sh importar` y `publicar`).
3. **Su dominio**: tema `spsil`, su logo y las rutas viejas (`stock>/paginas/`,
   `auth>https://users.<dominio>`), y **Poner en su dominio**. El portal viejo queda
   parado, no borrado, para poder volver con «Quitar su dominio».
4. **Después, con el sí del responsable**: pasar los usuarios a `/PRO/SPSIL/…`, quitar las
   apps sueltas que ya no hacen falta y parar el portal viejo (`docker compose rm -s
   portal`, nunca `down`, porque ahí está Keycloak).

**En curso:** los pasos 2 a 4 en PRO y el cambio de grupos de SPSIL. El estado real
lo dice Ajustes.

Para un producto **nuevo, sin nada de antes**, solo hacen falta los apartados 1 a 3.
