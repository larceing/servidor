# CLAUDE.md

The shared part of a DEV/PRO server, kept apart from every app: per-environment nginx
"entrada", the portada (`/`) and Ajustes (`/ajustes/`), Keycloak groups/clients per app,
own domains via Nginx Proxy Manager, and image-based apps. README.md (Spanish) is the
user doc and the `servidor.json` manifest spec. UI text and docs are in Spanish.

- `servidor.py`: stdlib-only Python 3, runs as root on the server. CLI + the systemd
  agent (`servidor agente`) that processes the panel's requests (`peticiones/*.json`),
  re-renders the entradas (validated with a throwaway `nginx -t`, rolled back if
  rejected), writes `estado/catalogo.json`, and self-updates from `main` only when the
  commit's GitHub checks are green (then rebuilds the panel image locally, rollback on
  unhealthy). Never use a shell: `run([...])` with arg lists; validate every field from
  a request or manifest with the regexes at the top.
- `panel/`: FastAPI app (portada + Ajustes). OIDC Authorization Code against Keycloak,
  session cookie `servidor_sesion`, CSRF token per session, IT = `GRUPO_IT`. Reads only
  `/estado`, writes only `/peticiones` and its `/datos` volume (GitHub token, Fernet).
  Deploys of workflow apps go straight to the GitHub API. `/ajustes/api/v1/` is the same
  deploy/status with a Bearer key (`claves.py`: sha256 only, DEV-only by default, per-app
  scope, expiry, audit log `/datos/api.log`); session cookies are never accepted there.
- Never hardcode a domain, server, person or app name: everything comes from
  `servidor.env` or an app's manifest. No app code lives here.
- Tests: `python -m unittest discover -s pruebas -v` and
  `(cd panel && python -m unittest discover -s tests -v)`. Docker/Keycloak/GitHub mocked.

Guide for adding a product or an app (for people and AIs): `docs/README.md`, then
`docs/NUEVA_APP.md` / `docs/NUEVO_PRODUCTO.md`; rules for agents in `AGENTS.md`. When a change
alters how apps plug in or deploy, update `docs/` in the same PR.
