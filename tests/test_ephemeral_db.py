import os
import subprocess
import sys
import textwrap
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))


def run_probe(extra_env):
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env.pop("ALLOW_EPHEMERAL_DB", None)
    env["RENDER"] = "true"
    env.update(extra_env)
    code = textwrap.dedent("""
        import database
        print(database.DATABASE_URL)
        print(database.EPHEMERAL_DB_MODE)
    """)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


class EphemeralDatabasePolicyTests(unittest.TestCase):
    def test_render_without_database_rejects_ephemeral_storage(self):
        result = run_probe({})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL must be configured on Render", result.stderr + result.stdout)

    def test_render_allows_ephemeral_only_with_explicit_flag(self):
        result = run_probe({"ALLOW_EPHEMERAL_DB": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sqlite:////tmp/dashle-ephemeral.db", result.stdout)
        self.assertIn("True", result.stdout)

    def test_database_url_has_priority_over_ephemeral_flag(self):
        env = os.environ.copy()
        env["RENDER"] = "true"
        env["DATABASE_URL"] = "sqlite:////tmp/should-not-be-used.db"
        env["ALLOW_EPHEMERAL_DB"] = "1"
        code = "import database; print(database.DATABASE_URL); print(database.EPHEMERAL_DB_MODE)"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Render must use the configured PostgreSQL database", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
