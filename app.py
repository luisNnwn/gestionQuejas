import hmac
import logging
import os
import re
import secrets
from functools import wraps
from json import JSONDecodeError
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import psycopg
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash


class DatabaseConfigurationError(Exception):
    pass


class N8NApiError(Exception):
    pass


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        DASHBOARD_USERNAME=os.environ.get("DASHBOARD_USERNAME", ""),
        DASHBOARD_PASSWORD=os.environ.get("DASHBOARD_PASSWORD", ""),
        N8N_CONTROL_TOKEN=os.environ.get("N8N_CONTROL_TOKEN", ""),
        N8N_API_BASE_URL=os.environ.get("N8N_API_BASE_URL", ""),
        N8N_WORKFLOW_ID=os.environ.get("N8N_WORKFLOW_ID", ""),
        N8N_API_KEY=os.environ.get("N8N_API_KEY", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    def db_connection():
        database_url = app.config["DATABASE_URL"]
        if not database_url:
            raise DatabaseConfigurationError("DATABASE_URL no está configurada.")
        return psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=5,
            prepare_threshold=None,
        )

    app.extensions["db_connection"] = db_connection

    def authenticated(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authenticated"):
                if request.path.startswith("/api/") or request.path.startswith("/internal/"):
                    return jsonify(error="Autenticación requerida."), 401
                return redirect(url_for("login", next=request.full_path))
            if not session.get("is_admin"):
                user_id = session.get("user_id")
                if not user_id:
                    session.clear()
                    if request.path.startswith("/api/") or request.path.startswith("/internal/"):
                        return jsonify(error="Autenticación requerida."), 401
                    return redirect(url_for("login", next=request.full_path))
                with app.extensions["db_connection"]() as connection:
                    account = connection.execute(
                        "SELECT activo FROM public.usuarios_panel WHERE id = %s",
                        (user_id,),
                    ).fetchone()
                if account is None or not account["activo"]:
                    session.clear()
                    if request.path.startswith("/api/") or request.path.startswith("/internal/"):
                        return jsonify(error="La cuenta está deshabilitada."), 401
                    return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    def admin_required(view):
        @wraps(view)
        @authenticated
        def wrapped(*args, **kwargs):
            if not session.get("is_admin"):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    def valid_csrf():
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        return bool(token and hmac.compare_digest(token, session.get("csrf_token", "")))

    def n8n_api_request(method, action=""):
        base_url = app.config["N8N_API_BASE_URL"].rstrip("/")
        workflow_id = app.config["N8N_WORKFLOW_ID"].strip()
        api_key = app.config["N8N_API_KEY"]
        if not base_url or not workflow_id or not api_key:
            raise N8NApiError(
                "Configura N8N_API_BASE_URL, N8N_WORKFLOW_ID y N8N_API_KEY."
            )
        action_paths = {"activate": "publish", "deactivate": "unpublish"}
        suffix = f"/{action_paths[action]}" if action else ""
        url = f"{base_url}/api/v1/workflows/{quote(workflow_id, safe='')}{suffix}"
        data = b"{}" if method == "POST" else None
        api_request = Request(
            url,
            data=data,
            headers={"X-N8N-API-KEY": api_key, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(api_request, timeout=10) as response:
                body = response.read()
        except HTTPError as error:
            app.logger.warning("n8n API rechazó la solicitud (HTTP %s).", error.code)
            raise N8NApiError(
                f"n8n rechazó la solicitud (HTTP {error.code})."
            ) from error
        except URLError as error:
            app.logger.error("No se pudo conectar con n8n: %s", error.reason)
            raise N8NApiError("No se pudo conectar con n8n.") from error
        if not body:
            return {}
        try:
            return app.json.loads(body)
        except (JSONDecodeError, UnicodeDecodeError) as error:
            app.logger.error("n8n devolvió una respuesta JSON inválida.")
            raise N8NApiError("n8n devolvió una respuesta no válida.") from error

    @app.before_request
    def ensure_csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)

    @app.errorhandler(psycopg.Error)
    def handle_database_error(error):
        app.logger.exception("Error de PostgreSQL", exc_info=error)
        if request.path.startswith("/api/") or request.path.startswith("/internal/"):
            return jsonify(error="No fue posible consultar la base de datos."), 503
        return (
            render_template(
                "error.html",
                title="Problema de conexión",
                message="No se pudo completar la operación con la base de datos. Intenta de nuevo.",
            ),
            503,
        )

    @app.errorhandler(DatabaseConfigurationError)
    def handle_database_configuration_error(error):
        app.logger.error("Configuración de base de datos incompleta: %s", error)
        if request.path.startswith("/api/") or request.path.startswith("/internal/"):
            return jsonify(error="La conexión a la base de datos no está configurada."), 503
        return (
            render_template(
                "error.html",
                title="Configuración incompleta",
                message="La aplicación necesita una URL de PostgreSQL para consultar los datos.",
            ),
            503,
        )

    @app.errorhandler(N8NApiError)
    def handle_n8n_api_error(error):
        app.logger.error("Error de integración con n8n: %s", error)
        if request.path.startswith("/api/"):
            return jsonify(error=str(error)), 502
        return (
            render_template(
                "error.html",
                title="No se pudo consultar n8n",
                message=str(error),
            ),
            502,
        )

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/readyz")
    def readiness():
        with app.extensions["db_connection"]() as connection:
            connection.execute("SELECT 1")
        return jsonify(status="ready")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if not valid_csrf():
                abort(400, description="Token CSRF inválido.")
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            admin_username = app.config["DASHBOARD_USERNAME"].strip().lower()
            admin_password = app.config["DASHBOARD_PASSWORD"]
            is_admin = bool(
                admin_username
                and admin_password
                and hmac.compare_digest(username, admin_username)
                and hmac.compare_digest(password, admin_password)
            )
            account = None
            if not is_admin and username != admin_username:
                with app.extensions["db_connection"]() as connection:
                    account = connection.execute(
                        """
                        SELECT id, username, nombre, password_hash, activo
                        FROM public.usuarios_panel
                        WHERE username = %s
                        """,
                        (username,),
                    ).fetchone()
                if (
                    account
                    and account["activo"]
                    and check_password_hash(account["password_hash"], password)
                ):
                    with app.extensions["db_connection"]() as connection:
                        connection.execute(
                            """
                            UPDATE public.usuarios_panel
                            SET ultimo_acceso_en = now()
                            WHERE id = %s
                            """,
                            (account["id"],),
                        )
                else:
                    account = None
            if is_admin or account:
                session.clear()
                session["authenticated"] = True
                session["is_admin"] = is_admin
                session["username"] = admin_username if is_admin else account["username"]
                if not is_admin:
                    session["user_id"] = str(account["id"])
                session["csrf_token"] = secrets.token_urlsafe(32)
                destination = request.args.get("next", "")
                if not destination.startswith("/") or destination.startswith("//"):
                    destination = url_for("dashboard")
                return redirect(destination)
            flash("Usuario o contraseña incorrectos.", "error")
        return render_template("login.html", csrf_token=session["csrf_token"])

    @app.post("/logout")
    @authenticated
    def logout():
        if not valid_csrf():
            abort(400, description="Token CSRF inválido.")
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @authenticated
    def dashboard():
        with app.extensions["db_connection"]() as connection:
            stats = connection.execute(
                """
                SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE recibido_en >= date_trunc('day', now())) AS hoy,
                    count(*) FILTER (
                        WHERE upper(estado) IN (
                            'RECIBIDO', 'ABIERTO', 'EN_PROCESO', 'ESCALADO', 'PENDIENTE_HUMANO'
                        )
                    ) AS pendientes,
                    count(*) FILTER (WHERE NULLIF(trim(respuesta), '') IS NOT NULL) AS con_respuesta
                FROM public.casos
                """
            ).fetchone()
            agent_stats = connection.execute(
                """
                SELECT a.codigo, a.nombre, a.nivel, a.activo,
                       count(DISTINCT e.caso_id) FILTER (
                           WHERE upper(e.estado) IN ('OK', 'COMPLETADO')
                       ) AS atendidas
                FROM public.agentes AS a
                LEFT JOIN public.ejecuciones_agentes AS e
                    ON e.agente_codigo = a.codigo
                GROUP BY a.id, a.codigo, a.nombre, a.nivel, a.activo
                ORDER BY a.nivel, a.nombre
                """
            ).fetchall()
            channel_stats = connection.execute(
                """
                SELECT c.codigo, c.nombre, c.activo, count(k.id) AS casos
                FROM public.canales AS c
                LEFT JOIN public.casos AS k ON upper(k.canal) = c.codigo
                GROUP BY c.id, c.codigo, c.nombre, c.activo
                ORDER BY c.nombre
                """
            ).fetchall()
            latest_cases = connection.execute(
                """
                SELECT id, canal, remitente_nombre, asunto, categoria, prioridad, agente_asignado,
                       estado, recibido_en
                FROM public.casos
                ORDER BY recibido_en DESC
                LIMIT 8
                """
            ).fetchall()
        max_agent_cases = max((agent["atendidas"] for agent in agent_stats), default=0)
        return render_template(
            "dashboard.html",
            stats=stats,
            agent_stats=agent_stats,
            max_agent_cases=max_agent_cases,
            channel_stats=channel_stats,
            latest_cases=latest_cases,
            csrf_token=session["csrf_token"],
        )

    @app.get("/casos")
    @authenticated
    def cases():
        selected_status = request.args.get("estado", "").strip()
        selected_channel = request.args.get("canal", "").strip().upper()
        allowed_channels = {"TELEGRAM", "GMAIL", "DISCORD", "WEB"}
        if selected_channel and selected_channel not in allowed_channels:
            abort(400, description="Canal no válido.")
        with app.extensions["db_connection"]() as connection:
            rows = connection.execute(
                """
                SELECT id, canal, remitente_nombre, remitente_id, asunto, categoria, prioridad,
                       agente_asignado, estado, respuesta, recibido_en, actualizado_en
                FROM public.casos
                WHERE (%s = '' OR estado = %s)
                  AND (%s = '' OR canal = %s)
                ORDER BY recibido_en DESC
                LIMIT 200
                """,
                (selected_status, selected_status, selected_channel, selected_channel),
            ).fetchall()
            states = connection.execute(
                "SELECT DISTINCT estado FROM public.casos ORDER BY estado"
            ).fetchall()
        return render_template(
            "cases.html",
            cases=rows,
            states=[row["estado"] for row in states],
            selected_status=selected_status,
            selected_channel=selected_channel,
        )

    @app.get("/casos/<uuid:case_id>")
    @authenticated
    def case_detail(case_id):
        with app.extensions["db_connection"]() as connection:
            case = connection.execute(
                "SELECT * FROM public.casos WHERE id = %s",
                (str(case_id),),
            ).fetchone()
            if case is None:
                abort(404)
            messages = connection.execute(
                """
                SELECT direccion, canal, contenido, id_externo, creado_en
                FROM public.mensajes
                WHERE caso_id = %s
                ORDER BY creado_en
                """,
                (str(case_id),),
            ).fetchall()
            executions = connection.execute(
                """
                SELECT agente_codigo, estado, error, inicio_en, fin_en
                FROM public.ejecuciones_agentes
                WHERE caso_id = %s
                ORDER BY inicio_en
                """,
                (str(case_id),),
            ).fetchall()
            events = connection.execute(
                """
                SELECT evento, detalle, creado_en
                FROM public.eventos_caso
                WHERE caso_id = %s
                ORDER BY creado_en
                """,
                (str(case_id),),
            ).fetchall()
        return render_template(
            "case_detail.html", case=case, messages=messages, executions=executions, events=events
        )

    @app.get("/modulos")
    @admin_required
    def modules():
        with app.extensions["db_connection"]() as connection:
            channels = connection.execute(
                "SELECT id, codigo, nombre, activo FROM public.canales ORDER BY nombre"
            ).fetchall()
            agents = connection.execute(
                "SELECT id, codigo, nombre, nivel, descripcion, activo FROM public.agentes ORDER BY nivel, nombre"
            ).fetchall()
        return render_template(
            "modules.html",
            channels=channels,
            agents=agents,
            csrf_token=session["csrf_token"],
        )

    @app.post("/modulos/canales/<codigo>/toggle")
    @admin_required
    def toggle_channel(codigo):
        if not valid_csrf():
            abort(400, description="Token CSRF inválido.")
        with app.extensions["db_connection"]() as connection:
            row = connection.execute(
                """
                UPDATE public.canales
                SET activo = NOT activo
                WHERE codigo = %s
                RETURNING codigo, activo
                """,
                (codigo.upper(),),
            ).fetchone()
            if row is None:
                abort(404)
        flash(
            f"Canal {row['codigo']}: {'habilitado' if row['activo'] else 'deshabilitado'}.",
            "success",
        )
        return redirect(url_for("modules"))

    @app.post("/modulos/agentes/<codigo>/toggle")
    @admin_required
    def toggle_agent(codigo):
        if not valid_csrf():
            abort(400, description="Token CSRF inválido.")
        with app.extensions["db_connection"]() as connection:
            row = connection.execute(
                """
                UPDATE public.agentes
                SET activo = NOT activo
                WHERE codigo = %s
                RETURNING codigo, activo
                """,
                (codigo.upper(),),
            ).fetchone()
            if row is None:
                abort(404)
        flash(
            f"Agente {row['codigo']}: {'habilitado' if row['activo'] else 'deshabilitado'}.",
            "success",
        )
        return redirect(url_for("modules"))

    @app.route("/usuarios", methods=["GET", "POST"])
    @admin_required
    def manage_users():
        generated_password = None
        if request.method == "POST":
            if not valid_csrf():
                abort(400, description="Token CSRF inválido.")
            username = request.form.get("username", "").strip().lower()
            full_name = request.form.get("nombre", "").strip()
            reserved_username = app.config["DASHBOARD_USERNAME"].strip().lower()
            if not re.fullmatch(r"[a-z0-9._-]{3,64}", username):
                flash("El usuario debe tener 3–64 caracteres: letras, números, punto, guion o guion bajo.", "error")
            elif username == reserved_username:
                flash("Ese nombre está reservado para la cuenta administradora.", "error")
            elif not full_name or len(full_name) > 120:
                flash("Ingresa un nombre de hasta 120 caracteres.", "error")
            else:
                generated_password = secrets.token_urlsafe(18)
                password_hash = generate_password_hash(generated_password)
                with app.extensions["db_connection"]() as connection:
                    created = connection.execute(
                        """
                        INSERT INTO public.usuarios_panel
                            (username, nombre, password_hash, creado_por)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (username) DO NOTHING
                        RETURNING id
                        """,
                        (username, full_name, password_hash, session["username"]),
                    ).fetchone()
                if created is None:
                    generated_password = None
                    flash("Ya existe una cuenta con ese nombre de usuario.", "error")
                else:
                    flash("Cuenta creada. Copia la contraseña temporal antes de salir de esta página.", "success")

        with app.extensions["db_connection"]() as connection:
            users = connection.execute(
                """
                SELECT id, username, nombre, activo, creado_en, ultimo_acceso_en
                FROM public.usuarios_panel
                ORDER BY creado_en DESC
                """
            ).fetchall()
        return render_template(
            "users.html",
            users=users,
            generated_password=generated_password,
            csrf_token=session["csrf_token"],
        )

    @app.post("/usuarios/<uuid:user_id>/toggle")
    @admin_required
    def toggle_user(user_id):
        if not valid_csrf():
            abort(400, description="Token CSRF inválido.")
        with app.extensions["db_connection"]() as connection:
            row = connection.execute(
                """
                UPDATE public.usuarios_panel
                SET activo = NOT activo
                WHERE id = %s
                RETURNING username, activo
                """,
                (str(user_id),),
            ).fetchone()
            if row is None:
                abort(404)
        flash(
            f"Cuenta {row['username']}: {'habilitada' if row['activo'] else 'deshabilitada'}.",
            "success",
        )
        return redirect(url_for("manage_users"))

    @app.get("/n8n")
    @admin_required
    def n8n_workflow():
        configured = all(
            app.config[key]
            for key in ("N8N_API_BASE_URL", "N8N_WORKFLOW_ID", "N8N_API_KEY")
        )
        workflow = n8n_api_request("GET") if configured else None
        if workflow is not None:
            if "activeVersionId" in workflow:
                workflow["is_active"] = workflow["activeVersionId"] is not None
            elif isinstance(workflow.get("active"), bool):
                workflow["is_active"] = workflow["active"]
            else:
                raise N8NApiError("n8n no devolvió el estado del workflow.")
        return render_template(
            "n8n.html",
            configured=configured,
            workflow=workflow,
            workflow_id=app.config["N8N_WORKFLOW_ID"],
            csrf_token=session["csrf_token"],
        )

    @app.post("/n8n/toggle")
    @admin_required
    def toggle_n8n_workflow():
        if not valid_csrf():
            abort(400, description="Token CSRF inválido.")
        action = request.form.get("action", "")
        if action not in {"activate", "deactivate"}:
            abort(400, description="Acción no válida.")
        n8n_api_request("POST", action)
        workflow = n8n_api_request("GET")
        active = (
            workflow["activeVersionId"] is not None
            if "activeVersionId" in workflow
            else workflow.get("active")
        )
        if active is not (action == "activate"):
            raise N8NApiError("n8n no confirmó el cambio de estado.")
        flash(
            "Workflow activado en n8n."
            if action == "activate"
            else "Workflow desactivado en n8n.",
            "success",
        )
        return redirect(url_for("n8n_workflow"))

    @app.get("/api/dashboard")
    @authenticated
    def dashboard_api():
        with app.extensions["db_connection"]() as connection:
            stats = connection.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE recibido_en >= date_trunc('day', now())) AS hoy,
                       count(*) FILTER (
                           WHERE upper(estado) IN (
                               'RECIBIDO', 'ABIERTO', 'EN_PROCESO', 'ESCALADO', 'PENDIENTE_HUMANO'
                           )
                       ) AS pendientes,
                       count(*) FILTER (WHERE NULLIF(trim(respuesta), '') IS NOT NULL) AS con_respuesta
                FROM public.casos
                """
            ).fetchone()
        return jsonify(stats)

    @app.get("/internal/config")
    def internal_config():
        expected_token = app.config["N8N_CONTROL_TOKEN"]
        provided_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not expected_token or not hmac.compare_digest(provided_token, expected_token):
            return jsonify(error="No autorizado."), 401
        with app.extensions["db_connection"]() as connection:
            channels = connection.execute(
                "SELECT codigo, activo FROM public.canales ORDER BY codigo"
            ).fetchall()
            agents = connection.execute(
                "SELECT codigo, nivel, activo FROM public.agentes ORDER BY nivel, codigo"
            ).fetchall()
        return jsonify(
            channels=[dict(row) for row in channels],
            agents=[dict(row) for row in agents],
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
