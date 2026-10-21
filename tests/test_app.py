import unittest

from app import create_app


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

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))
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


if __name__ == "__main__":
    unittest.main()
