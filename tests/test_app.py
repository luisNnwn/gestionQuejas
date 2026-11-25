import re
import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from werkzeug.security import check_password_hash, generate_password_hash


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, *args, **kwargs):
        self.executed = []
        self.users = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if "SELECT activo FROM public.usuarios_panel" in query:
            return Result(next(
                ({"activo": user["activo"]} for user in self.users.values() if user["id"] == params[0]),
                None,
            ))
        if "SELECT id, username, nombre, password_hash, activo" in query:
            return Result(self.users.get(params[0]))
        if "INSERT INTO public.usuarios_panel" in query:
            username, nombre, password_hash, creado_por = params
            if username in self.users:
                return Result()
            user = {
                "id": "47e01a6e-8441-4bf3-8e10-3d3f26c86954",
                "username": username,
                "nombre": nombre,
                "password_hash": password_hash,
                "activo": True,
                "creado_por": creado_por,
                "creado_en": None,
                "ultimo_acceso_en": None,
            }
            self.users[username] = user
            return Result({"id": user["id"]})
        if "SELECT id, username, nombre, activo, creado_en, ultimo_acceso_en" in query:
            return Result(rows=list(self.users.values()))
        if "UPDATE public.usuarios_panel" in query and "SET activo = NOT activo" in query:
            user = next((user for user in self.users.values() if user["id"] == params[0]), None)
            if user:
                user["activo"] = not user["activo"]
                return Result({"username": user["username"], "activo": user["activo"]})
            return Result()
        if "UPDATE public.canales" in query:
            return Result({"codigo": "TELEGRAM", "activo": False})
        if "UPDATE public.agentes" in query:
            return Result({"codigo": "RESOLUTOR", "activo": False})
        if "SELECT codigo, activo FROM public.canales" in query:
            return Result(rows=[{"codigo": "TELEGRAM", "activo": True}])
        if "SELECT codigo, nivel, activo FROM public.agentes" in query:
            return Result(rows=[{"codigo": "RESOLUTOR", "nivel": 1, "activo": True}])
        if "SELECT id, codigo, nombre, activo FROM public.canales" in query:
            return Result(rows=[{
                "id": 1,
                "codigo": "TELEGRAM",
                "nombre": "Telegram",
                "activo": True,
            }])
        if "SELECT id, codigo, nombre, nivel, descripcion, activo FROM public.agentes" in query:
            return Result(rows=[{
                "id": 1,
                "codigo": "RESOLUTOR",
                "nombre": "Resolutor",
                "nivel": 1,
                "descripcion": "Resuelve consultas",
                "activo": True,
            }])
        if "count(*) FILTER" in query:
            return Result({"total": 4, "hoy": 2, "pendientes": 1, "con_respuesta": 3})
        if "FROM public.agentes AS a" in query:
            return Result(rows=[{
                "codigo": "RESOLUTOR",
                "nombre": "Resolutor",
                "nivel": 1,
                "activo": True,
                "atendidas": 2,
            }])
        if "FROM public.canales AS c" in query:
            return Result(rows=[{
                "codigo": "TELEGRAM",
                "nombre": "Telegram",
                "activo": True,
                "casos": 4,
            }])
        return Result()


class DashboardAppTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "safe-test-password",
                "N8N_CONTROL_TOKEN": "internal-test-token",
                "SESSION_COOKIE_SECURE": False,
            }
        )
        self.app.extensions["db_connection"] = lambda: self.connection
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as user_session:
            user_session["authenticated"] = True
            user_session["is_admin"] = True
            user_session["username"] = "admin"
            user_session["csrf_token"] = "valid-csrf-token"

    def test_health_endpoint_is_available_without_database(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_readiness_reports_missing_database_configuration(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        response = app.test_client().get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Configuraci", response.data)

    def test_dashboard_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_rejects_invalid_csrf_token(self):
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "safe-test-password", "csrf_token": "bad"},
        )
        self.assertEqual(response.status_code, 400)

    def test_internal_config_requires_bearer_token(self):
        response = self.client.get("/internal/config")
        self.assertEqual(response.status_code, 401)

    def test_internal_config_returns_enabled_modules_to_n8n(self):
        response = self.client.get(
            "/internal/config",
            headers={"Authorization": "Bearer internal-test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["channels"][0]["codigo"], "TELEGRAM")
        self.assertTrue(response.get_json()["agents"][0]["activo"])

    def test_dashboard_renders_metrics_and_agent_summary(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Casos registrados", response.data)
        self.assertIn(b"Resolutor", response.data)
        self.assertIn("PENDIENTE_HUMANO", self.connection.executed[0][0])

    def test_modules_page_renders_database_controls(self):
        self.login()
        response = self.client.get("/modulos")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Deshabilitar", response.data)

    def test_login_accepts_valid_credentials(self):
        with self.client.session_transaction() as user_session:
            user_session["csrf_token"] = "valid-csrf-token"
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "safe-test-password",
                "csrf_token": "valid-csrf-token",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as user_session:
            self.assertTrue(user_session["authenticated"])
            self.assertTrue(user_session["is_admin"])

    def test_channel_toggle_checks_csrf_and_updates_flag(self):
        self.login()
        response = self.client.post(
            "/modulos/canales/telegram/toggle",
            data={"csrf_token": "valid-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("UPDATE public.canales", self.connection.executed[0][0])
        self.assertEqual(len(self.connection.executed), 1)

    def test_agent_toggle_rejects_missing_csrf_token(self):
        self.login()
        response = self.client.post("/modulos/agentes/resolutor/toggle")
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_open_admin_modules(self):
        self.connection.users["reader"] = {
            "id": "47e01a6e-8441-4bf3-8e10-3d3f26c86955",
            "username": "reader",
            "nombre": "Reader",
            "password_hash": generate_password_hash("reader-password"),
            "activo": True,
        }
        with self.client.session_transaction() as user_session:
            user_session["authenticated"] = True
            user_session["is_admin"] = False
            user_session["user_id"] = self.connection.users["reader"]["id"]
            user_session["csrf_token"] = "valid-csrf-token"
        response = self.client.get("/modulos")
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_toggle_channel_even_with_csrf(self):
        self.connection.users["reader"] = {
            "id": "47e01a6e-8441-4bf3-8e10-3d3f26c86955",
            "username": "reader",
            "nombre": "Reader",
            "password_hash": generate_password_hash("reader-password"),
            "activo": True,
        }
        with self.client.session_transaction() as user_session:
            user_session["authenticated"] = True
            user_session["is_admin"] = False
            user_session["user_id"] = self.connection.users["reader"]["id"]
            user_session["csrf_token"] = "valid-csrf-token"
        response = self.client.post(
            "/modulos/canales/telegram/toggle",
            data={"csrf_token": "valid-csrf-token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(any("UPDATE public.canales" in query for query, _ in self.connection.executed))

    def test_admin_can_create_viewer_with_one_time_generated_password(self):
        self.login()
        response = self.client.post(
            "/usuarios",
            data={
                "username": "consulta1",
                "nombre": "Usuario de consulta",
                "csrf_token": "valid-csrf-token",
            },
        )
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'id="generated-password"[^>]*value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        password = match.group(1).decode()
        saved = self.connection.users["consulta1"]
        self.assertTrue(check_password_hash(saved["password_hash"], password))
        self.assertNotEqual(saved["password_hash"], password)
        self.assertIn("Contraseña copiada".encode(), response.data)

    def test_regular_user_login_uses_hashed_database_password(self):
        self.connection.users["consulta1"] = {
            "id": "47e01a6e-8441-4bf3-8e10-3d3f26c86956",
            "username": "consulta1",
            "nombre": "Usuario de consulta",
            "password_hash": generate_password_hash("generated-password"),
            "activo": True,
        }
        with self.client.session_transaction() as user_session:
            user_session["csrf_token"] = "valid-csrf-token"
        response = self.client.post(
            "/login",
            data={
                "username": "consulta1",
                "password": "generated-password",
                "csrf_token": "valid-csrf-token",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as user_session:
            self.assertTrue(user_session["authenticated"])
            self.assertFalse(user_session["is_admin"])
            self.assertEqual(user_session["user_id"], self.connection.users["consulta1"]["id"])

    def test_disabled_viewer_session_is_revoked(self):
        self.connection.users["reader"] = {
            "id": "47e01a6e-8441-4bf3-8e10-3d3f26c86955",
            "username": "reader",
            "nombre": "Reader",
            "password_hash": generate_password_hash("reader-password"),
            "activo": False,
        }
        with self.client.session_transaction() as user_session:
            user_session["authenticated"] = True
            user_session["is_admin"] = False
            user_session["user_id"] = self.connection.users["reader"]["id"]
        response = self.client.get("/casos")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with self.client.session_transaction() as user_session:
            self.assertNotIn("authenticated", user_session)

    def test_viewer_cannot_open_n8n_controls(self):
        self.connection.users["reader"] = {
            "id": "47e01a6e-8441-4bf3-8e10-3d3f26c86955",
            "username": "reader",
            "nombre": "Reader",
            "password_hash": generate_password_hash("reader-password"),
            "activo": True,
        }
        with self.client.session_transaction() as user_session:
            user_session["authenticated"] = True
            user_session["is_admin"] = False
            user_session["user_id"] = self.connection.users["reader"]["id"]
            user_session["csrf_token"] = "valid-csrf-token"
        response = self.client.get("/n8n")
        self.assertEqual(response.status_code, 403)

    def test_admin_can_activate_configured_workflow(self):
        self.login()
        self.app.config.update(
            N8N_API_BASE_URL="https://n8n.example",
            N8N_WORKFLOW_ID="workflow-id",
            N8N_API_KEY="test-api-key",
        )
        post_response = MagicMock()
        post_response.read.return_value = b""
        post_response.__enter__.return_value = post_response
        get_response = MagicMock()
        get_response.read.return_value = (
            b'{"active": false, "activeVersionId": "published-version"}'
        )
        get_response.__enter__.return_value = get_response
        with patch("app.urlopen", side_effect=[post_response, get_response]) as api_call:
            response = self.client.post(
                "/n8n/toggle",
                data={"action": "activate", "csrf_token": "valid-csrf-token"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(api_call.call_count, 2)
        api_request = api_call.call_args_list[0].args[0]
        self.assertEqual(api_request.full_url, "https://n8n.example/api/v1/workflows/workflow-id/publish")
        self.assertEqual(api_request.method, "POST")
        self.assertEqual(api_request.get_header("X-n8n-api-key"), "test-api-key")
        self.assertEqual(
            api_call.call_args_list[1].args[0].full_url,
            "https://n8n.example/api/v1/workflows/workflow-id",
        )

    def test_admin_can_deactivate_configured_workflow(self):
        self.login()
        self.app.config.update(
            N8N_API_BASE_URL="https://n8n.example",
            N8N_WORKFLOW_ID="workflow-id",
            N8N_API_KEY="test-api-key",
        )
        post_response = MagicMock()
        post_response.read.return_value = b""
        post_response.__enter__.return_value = post_response
        get_response = MagicMock()
        get_response.read.return_value = b'{"activeVersionId": null}'
        get_response.__enter__.return_value = get_response
        with patch("app.urlopen", side_effect=[post_response, get_response]) as api_call:
            response = self.client.post(
                "/n8n/toggle",
                data={"action": "deactivate", "csrf_token": "valid-csrf-token"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            api_call.call_args_list[0].args[0].full_url,
            "https://n8n.example/api/v1/workflows/workflow-id/unpublish",
        )


if __name__ == "__main__":
    unittest.main()
