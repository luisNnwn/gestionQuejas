# UFG · Panel de atención

Aplicación Flask para consultar casos y operar las banderas de disponibilidad de canales y agentes en PostgreSQL. El esquema inicial está en `supabase/schema.sql`; revísalo y ejecútalo en el SQL Editor del proyecto Supabase que realmente utiliza n8n. La app no aplica migraciones automáticamente. Si ya hay casos duplicados por canal y `mensaje_externo_id`, el script se detendrá antes de crear el índice único; hay que resolver esos duplicados antes de repetirlo.

## Desarrollo local

1. Crear y activar un entorno virtual.
2. Instalar dependencias con `pip install -r requirements.txt`.
3. Copiar `.env.example` a `.env` y definir `DATABASE_URL`, `FLASK_SECRET_KEY`, `DASHBOARD_USERNAME` y `DASHBOARD_PASSWORD`. Para que el administrador controle la activación del workflow desde el panel, configurar también `N8N_API_BASE_URL`, `N8N_WORKFLOW_ID` y `N8N_API_KEY`.
4. Ejecutar `flask --app app run --debug`.

El servidor está disponible en `http://127.0.0.1:5000`. La ruta `/healthz` verifica que la app inició; `/readyz` también prueba PostgreSQL.

## Despliegue propuesto en Google Cloud

- Desplegar Flask en **Cloud Run** desde este `Dockerfile`. Configurar secretos (`FLASK_SECRET_KEY`, `DASHBOARD_PASSWORD`, `DATABASE_URL`, `N8N_CONTROL_TOKEN`, `N8N_API_KEY`) en Secret Manager y asignarlos como variables de entorno del servicio. `DASHBOARD_USERNAME`, `N8N_API_BASE_URL` y `N8N_WORKFLOW_ID` son configuración no secreta. No guardar `.env` ni credenciales en el repositorio.
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
- `N8N_API_KEY`: clave de API de n8n con permisos `workflow:read`, `workflow:publish` y `workflow:deactivate` para consultar/publicar/despublicar el workflow desde el panel. Guárdala únicamente en Secret Manager y limita sus permisos a esas operaciones.

Configura además `N8N_API_BASE_URL` con la URL base de la instancia (sin `/api/v1`) y `N8N_WORKFLOW_ID` con el ID del workflow que se controlará. Ambos valores se pueden cambiar al migrar n8n de Cloud a una VM; no compartas ni guardes la clave de API en el repositorio.

No compartas los valores en el chat, no los guardes en el repositorio y no reutilices la contraseña de Supabase como contraseña del panel. Después, al crear/editar el servicio Cloud Run, ve a **Variables y secretos → Referenciar un secreto**, asígnalos a variables con el mismo nombre y usa la identidad de servicio del servicio. Dale a esa cuenta el rol **Secret Manager Secret Accessor** sobre esos secretos. Configura también `COOKIE_SECURE=true`.

La aplicación se desplegará en Cloud Run; el servicio n8n requiere por separado una VM Compute Engine persistente. No expongas el puerto 5678 directamente a Internet. El despliegue desde esta estación todavía requiere instalar Google Cloud CLI (`gcloud`) y autenticarlo con una cuenta que tenga permisos en el proyecto.

## Rutas del panel

- `/`: métricas generales, rendimiento registrado por agente, estado de canales y casos recientes.
- `/casos`: listado con filtros de canal/estado y límite de 200 filas.
- `/casos/<uuid>`: mensaje, respuesta, ejecuciones de agentes, mensajes y eventos relacionados.
- `/modulos`: habilitar/deshabilitar canales y agentes.
- `/usuarios`: administración de cuentas de consulta; solo el administrador puede crear o deshabilitar cuentas.
- `/n8n`: estado y control de activación del workflow; solo el administrador puede usarlo.
- `/api/dashboard`: métricas JSON, protegidas por la sesión del panel.
- `/internal/config`: configuración activa para n8n; requiere `Authorization: Bearer <N8N_CONTROL_TOKEN>`.

El usuario administrador inicia sesión con `DASHBOARD_USERNAME` y `DASHBOARD_PASSWORD` configurados desde Secret Manager. Las cuentas adicionales se guardan en `public.usuarios_panel`; la aplicación genera una contraseña aleatoria y muestra el texto en claro únicamente en la respuesta de creación. La base guarda solo el hash. Estas cuentas pueden consultar casos, pero no administrar usuarios, módulos ni activar/desactivar workflows. Deshabilitar una cuenta revoca su sesión en la siguiente solicitud.

Para Cloud Run, configura `DATABASE_URL` copiando la cadena que entrega **Supabase → Connect → Transaction pooler**, reemplazando `[YOUR-PASSWORD]` con la contraseña rotada y añadiendo `?sslmode=require`. El puerto `6543` corresponde al modo transaccional; Psycopg está configurado para desactivar prepared statements, que ese modo no soporta. Usa siempre el host/usuario de la cadena que copia Supabase. No incluyas la URI en el repositorio ni la compartas en el chat; guárdala en Secret Manager.

Las métricas “con respuesta” cuentan casos con `respuesta` no vacía; “atención por agente” cuenta casos distintos con una ejecución del agente en estado `OK` o `COMPLETADO`. No representan necesariamente que una queja se haya resuelto. Para desarrollo local, usar `COOKIE_SECURE=false`; en producción HTTPS debe estar en `true`.

## Integración con el workflow n8n

Los controles del panel guardan `canales.activo` y `agentes.activo` en Supabase. El workflow nuevo lee esos mismos valores antes de procesar canales y niveles de atención, y escribe los casos, mensajes, eventos y ejecuciones usando las columnas de `supabase/schema.sql`. Se retiró el panel separado que estaba embebido en n8n para centralizar autenticación y permisos en Flask; sus endpoints no protegían del lado del servidor las operaciones de configuración/casos. `/internal/config` sigue disponible para integraciones futuras.

El workflow n8n debe permanecer **inactivo hasta probarlo contra PostgreSQL y revisar sus credenciales**. Ejecuta `supabase/schema.sql` en el proyecto correcto y confirma que la credencial `Postgres account` apunta al mismo Supabase. Los controles de canal omiten mensajes de canales inactivos, pero no desactivan físicamente cada trigger: Discord tiene entradas por webhook y sondeo dentro del mismo workflow. El control de `/n8n` activa o desactiva el workflow completo.

## Validación

Ejecutar `python -m unittest discover -s tests`.
