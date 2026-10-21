import hmac
import logging
import os
import secrets
from functools import wraps

import psycopg
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from psycopg.rows import dict_row


class DatabaseConfigurationError(Exception):
    pass


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        DASHBOARD_USERNAME=os.environ.get("DASHBOARD_USERNAME", ""),
        DASHBOARD_PASSWORD=os.environ.get("DASHBOARD_PASSWORD", ""),
        N8N_CONTROL_TOKEN=os.environ.get("N8N_CONTROL_TOKEN", ""),
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
            return view(*args, **kwargs)

        return wrapped

    def valid_csrf():
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        return bool(token and hmac.compare_digest(token, session.get("csrf_token", "")))

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
            username = app.config["DASHBOARD_USERNAME"]
            password = app.config["DASHBOARD_PASSWORD"]
            if not username or not password:
                abort(503, description="Configura DASHBOARD_USERNAME y DASHBOARD_PASSWORD.")
            user_matches = hmac.compare_digest(request.form.get("username", ""), username)
            password_matches = hmac.compare_digest(request.form.get("password", ""), password)
            if user_matches and password_matches:
                session.clear()
                session["authenticated"] = True
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
    @authenticated
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
    @authenticated
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
    @authenticated
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
