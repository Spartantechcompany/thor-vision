# Gobierno del proyecto: Jira, Confluence y COBIT

Estado verificado el 2026-10-03 (solo lectura) y propuesta de alineación. Nada de esto está implementado todavía en Jira.

## Estado real

- **Jira:** conectado (`oxmtech.atlassian.net`) con permisos de lectura y escritura de trabajo (`read:jira-work`, `write:jira-work`).
- **Confluence:** **no está conectado ni se detectó espacio**. La conexión solo tiene el permiso de Jira, así que no se puede leer ni publicar páginas. Hasta que exista el espacio, la documentación vive en este repo (`docs/`).
- **Proyectos Jira existentes (6):** `CPPS` (podcast), `DDINO` (Dirección de Inteligencia de Negocios), `DIN1` (DIN - OXM Tech), `NET` (Netcs), `OTSDTEI` (TI e Innovación) y `TICKET` (Soporte OXM Tech, de tipo service desk).
- `TICKET` ya trae los tipos ITSM: **Incident, Problem, Change, Post-incident review y Service request (con aprobaciones)**. Es la base natural para COBIT DSS y BAI.
- **No existe un proyecto o épica de THOR Vision.**

## Mapa COBIT 2019 de este proyecto

| Proceso COBIT | Qué es aquí | Dónde queda registrado |
|---|---|---|
| BAI06 Cambios | PR, versión, CHANGELOG, GitHub Release | PR + tag `vX.Y.Z` + `release_notes.json` |
| BAI07 Aceptación y transición | Traspaso a Diego, pruebas | `docs/HANDOFF.md`, CI |
| BAI10 Configuración | Versiones desplegadas, componentes, imagen base | versión y commit en el dashboard (`/api/version`) |
| DSS01 Operaciones | Procedimientos de operación | `CLAUDE.md`, `docs/HANDOFF.md` |
| DSS02 Incidentes | Fallas del gateway, desbordamiento de contexto, caídas | Jira `Incident` en TICKET |
| DSS03 Problemas | Causa raíz y acción correctiva | Jira `Problem` + `Post-incident review` |
| DSS05 Seguridad | Credenciales, escaneo de secretos, retención | gitleaks en el CI, rotación de llaves |
| APO12 Riesgos | Biometría, falsos positivos, dependencias cruzadas | registro de riesgos (pendiente) |
| APO14 Datos | Retención de capturas (90 días), categorías de personas | `CAPTURE_TTL_DAYS`, `GUEST_TTL_DAYS` |
| MEA01 Monitoreo | Latencia del VLM, errores por código | Grafana |

El punto más sensible es la **biometría** (rostros y vectores faciales): falta documentar finalidad, base legal y retención en una evaluación de impacto. Es lo primero que revisaría una auditoría (APO12/APO14).

## Flujo propuesto

1. Todo cambio a producción tiene una clave Jira (tipo `Change`, aprobado) y un PR que la menciona.
2. Cada release (`vX.Y.Z`) enlaza su `Change`; las notas salen de `release_notes.json`.
3. Cada falla en operación es un `Incident`; si tiene causa raíz, se abre un `Problem` y su `Post-incident review`.
4. Riesgos y datos sensibles en un registro de riesgos; revisión trimestral.
5. Cuando exista Confluence: páginas de runbooks, notas de versión, registro de riesgos, evaluación de impacto de biometría y esta matriz.

## Reglas de privacidad

Nada de credenciales, URLs RTSP, capturas ni datos de personas se sube a Jira ni a Confluence. Solo identificadores, conteos y descripciones genéricas.

## Decisiones pendientes

- Dónde vive el trabajo: una épica "THOR Vision" en `OTSDTEI` o un proyecto nuevo (requiere permisos de administración).
- Crear el espacio de Confluence y dar el permiso a la conexión.
- Si los cambios de producción pasan por un comité de cambios (CAB) o por aprobación de una persona.
- Si se adopta RaiSE: su adaptador de Jira necesita el binario `acli`, que no está instalado. Ver la evaluación del 2026-10-03 (no recomendado hoy por el peso y el residuo que deja).
