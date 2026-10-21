# UFG · Panel de atención

Aplicación Flask para consultar casos y operar las banderas de disponibilidad de canales y agentes en PostgreSQL. El esquema inicial está en `supabase/schema.sql`; revísalo y ejecútalo en el SQL Editor del proyecto Supabase que realmente utiliza n8n. La app no aplica migraciones automáticamente.

## Desarrollo local

1. Crear y activar un entorno virtual.
2. Instalar dependencias con `pip install -r requirements.txt`.
3. Copiar `.env.example` a `.env` y definir `DATABASE_URL`, `FLASK_SECRET_KEY`, `DASHBOARD_USERNAME` y `DASHBOARD_PASSWORD`.
4. Ejecutar `flask --app app run --debug`.

El servidor está disponible en `http://127.0.0.1:5000`. La ruta `/healthz` verifica que la app inició; `/readyz` también prueba PostgreSQL.

## Despliegue propuesto en Google Cloud

- Desplegar Flask en **Cloud Run** desde este `Dockerfile`. Configurar secretos (`FLASK_SECRET_KEY`, `DASHBOARD_PASSWORD`, `DATABASE_URL`, `N8N_CONTROL_TOKEN`) en Secret Manager y asignarlos como variables de entorno del servicio. No guardar `.env` ni credenciales en el repositorio.
- Restringir el acceso al servicio de Cloud Run mediante IAM/IAP además de la sesión propia del panel; establecer `COOKIE_SECURE=true` para HTTPS.
- Ejecutar n8n en una VM de **Compute Engine** con Docker Compose desde `deploy/docker-compose.yml`. Configurar `N8N_HOST`, `WEBHOOK_URL` y `N8N_ENCRYPTION_KEY` en el entorno de la VM; persistir el volumen `n8n_data` y proteger la VM con firewall.
- Publicar el editor y los webhooks de n8n detrás de HTTPS (por ejemplo, proxy inverso o balanceador). El puerto 5678 está enlazado a localhost para que no se exponga directamente.
- La API Flask debe tener conectividad de red privada o segura hacia la base PostgreSQL. Si se usa Cloud SQL, configurar conectividad Cloud Run–Cloud SQL y un usuario de mínimo privilegio. Si se conserva Supabase, guardar su URI en Secret Manager y usar TLS.
- Configure una política de copia de seguridad y recuperación para la base del flujo y los datos persistentes de n8n. La actualización `stable` de la imagen debe fijarse a una versión validada en producción.

### Crear secretos para Cloud Run

En la consola de Google Cloud, selecciona el proyecto `sistemaautomatizadodequejas` y abre [Seguridad → Secret Manager](https://console.cloud.google.com/security/secret-manager?project=sistemaautomatizadodequejas) → **Crear secreto**. Crea al menos estos secretos, cada uno por separado:

- `DATABASE_URL`: URI PostgreSQL del **Session pooler** de Supabase con la contraseña nueva y `sslmode=require`. Usa el formato `postgresql://USUARIO:CONTRASEÑA@HOST:PUERTO/postgres?sslmode=require`. Si la contraseña contiene caracteres especiales, codifícalos para URL antes de armar la URI.
- `FLASK_SECRET_KEY`: valor aleatorio largo para firmar sesiones Flask.
- `DASHBOARD_PASSWORD`: contraseña nueva y exclusiva del panel.
- `DASHBOARD_USERNAME`: usuario que elijas para el panel.
- `N8N_CONTROL_TOKEN`: valor aleatorio largo para autenticar las consultas del workflow a Flask, si se usa `/internal/config`.

No compartas los valores en el chat, no los guardes en el repositorio y no reutilices la contraseña de Supabase como contraseña del panel. Después, al crear/editar el servicio Cloud Run, ve a **Variables y secretos → Referenciar un secreto**, asígnalos a variables con el mismo nombre y usa la identidad de servicio del servicio. Dale a esa cuenta el rol **Secret Manager Secret Accessor** sobre esos secretos. Configura también `COOKIE_SECURE=true`.

La aplicación se desplegará en Cloud Run; el servicio n8n requiere por separado una VM Compute Engine persistente. No expongas el puerto 5678 directamente a Internet. El despliegue desde esta estación todavía requiere instalar Google Cloud CLI (`gcloud`) y autenticarlo con una cuenta que tenga permisos en el proyecto.

## Rutas del panel

- `/`: métricas generales, rendimiento registrado por agente, estado de canales y casos recientes.
- `/casos`: listado con filtros de canal/estado y límite de 200 filas.
- `/casos/<uuid>`: mensaje, respuesta, ejecuciones de agentes, mensajes y eventos relacionados.
- `/modulos`: habilitar/deshabilitar canales y agentes.
- `/api/dashboard`: métricas JSON, protegidas por la sesión del panel.
- `/internal/config`: configuración activa para n8n; requiere `Authorization: Bearer <N8N_CONTROL_TOKEN>`.

Para Cloud Run, configura `DATABASE_URL` copiando la cadena que entrega **Supabase → Connect → Transaction pooler**, reemplazando `[YOUR-PASSWORD]` con la contraseña rotada y añadiendo `?sslmode=require`. El puerto `6543` corresponde al modo transaccional; Psycopg está configurado para desactivar prepared statements, que ese modo no soporta. Usa siempre el host/usuario de la cadena que copia Supabase. No incluyas la URI en el repositorio ni la compartas en el chat; guárdala en Secret Manager.

Las métricas “con respuesta” cuentan casos con `respuesta` no vacía; “atención por agente” cuenta casos distintos con una ejecución del agente en estado `OK` o `COMPLETADO`. No representan necesariamente que una queja se haya resuelto. Para desarrollo local, usar `COOKIE_SECURE=false`; en producción HTTPS debe estar en `true`.

## Integración con el workflow n8n

Los botones guardan `canales.activo` y `agentes.activo`. El borrador del workflow consulta la misma base para saltar canales deshabilitados y omitir los agentes deshabilitados; registra los agentes habilitados que participaron, guarda caso/mensajes/eventos y registra ejecuciones antes de enviar la respuesta. Si no hay agente disponible, el caso queda pendiente para atención humana. La ruta `/internal/config` sigue disponible para futuros clientes que necesiten leer el estado desde Flask.

El workflow n8n sigue **inactivo y sin probar contra PostgreSQL**. Antes de activarlo, ejecuta `supabase/schema.sql` en el proyecto correcto, rota cualquier clave que se haya compartido fuera de un gestor seguro y confirma en n8n que la credencial `Postgres account` apunta al mismo proyecto Supabase. Este control omite mensajes de canales inactivos, pero no apaga los triggers: Discord tiene entradas por webhook y sondeo dentro del mismo workflow. Para detener físicamente cada trigger desde el panel, separa los canales en workflows independientes.

## Validación

Ejecutar `python -m unittest discover -s tests`.
