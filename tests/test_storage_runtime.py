"""Regression checks for the web service's persistent storage selection."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StorageRuntimeTests(unittest.TestCase):
    def run_python(self, script, overrides=None):
        env = os.environ.copy()
        env.pop("DATABASE_URL", None)
        env.pop("RENDER", None)
        env.update(overrides or {})
        return subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_health_checks_sql_and_fails_closed(self):
        result = self.run_python(
            """
import web, database
from unittest.mock import patch
client = web.app.test_client()
assert web.session_base is database.session_base
ok = client.get('/health')
assert ok.status_code == 200 and ok.get_json() == {'ok': True, 'service': 'dashle'}
with patch.object(web, 'session_base', side_effect=RuntimeError('private connection detail')):
    failed = client.get('/health')
assert failed.status_code == 503
assert failed.get_json() == {'ok': False, 'service': 'dashle'}
assert b'private connection detail' not in failed.data
""",
            {"DATABASE_URL": "sqlite://"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_connected_records_use_shared_sql_engine_not_json_memory(self):
        result = self.run_python(
            """
import web, database, memory
from unittest.mock import patch
from database import Base, User, Conversation, Message, UserMemory, UserPreference, session_base
models = (User, Conversation, Message, UserMemory, UserPreference)
assert all(model.__table__.metadata is Base.metadata for model in models)
assert web.session_base is database.session_base
with session_base() as db:
    user = User(email='storage-check@example.invalid', password_hash='test-only')
    db.add(user); db.flush(); uid = user.id
    conversation = Conversation(user_id=uid, title='test fixture')
    db.add(conversation); db.flush(); cid = conversation.id
with patch.object(memory, 'charger_memoire', side_effect=AssertionError('JSON memory read')):
    with patch.object(memory, 'sauvegarder_memoire', side_effect=AssertionError('JSON memory write')):
        memory.retenir('test-key', 'SQL value', uid)
        assert memory.se_souvenir('test-key', uid) == 'SQL value'
        assert memory.se_souvenir_tout(uid)['test-key'] == 'SQL value'
web.ajouter_message(uid, cid, 'SQL message', 'user')
assert web._preferences(uid)['theme'] == 'clair'
with session_base() as db:
    assert db.query(Message).filter_by(conversation_id=cid, texte='SQL message').count() == 1
    assert db.query(UserPreference).filter_by(user_id=uid).count() == 1
""",
            {"DATABASE_URL": "sqlite://"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_render_rejects_missing_or_sqlite_database_url(self):
        missing = self.run_python("import database", {"RENDER": "true"})
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("DATABASE_URL must be configured on Render", missing.stderr)

        sqlite = self.run_python(
            "import database",
            {"RENDER": "true", "DATABASE_URL": "sqlite:///must-not-be-used.db"},
        )
        self.assertNotEqual(sqlite.returncode, 0)
        self.assertIn("must use the configured PostgreSQL database", sqlite.stderr)

    def test_render_accepts_postgresql_url_without_connecting(self):
        result = self.run_python(
            "import database; assert database.engine.dialect.name == 'postgresql'",
            {
                "RENDER": "true",
                "DATABASE_URL": "postgresql+psycopg://local:fake@127.0.0.1:1/dashle",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
