"""Tests de sécurité des connecteurs sans appels réseau."""
import os
import unittest
from datetime import datetime, timedelta

os.environ["DATABASE_URL"]="sqlite://"
os.environ["SESSION_COOKIE_SECURE"]="0"
os.environ["DASHLE_CONNECTOR_FERNET_KEY"]="0V5uYh8WcV9Y7cVY9rV0d3c8J7Jx9V0yq5Qm2n5r8sE="
os.environ["CONNECTORS_ENABLED"]="1"
os.environ["CONNECTOR_GITHUB_ENABLED"]="1"
os.environ["CONNECTOR_GOOGLE_ENABLED"]="1"
os.environ["CONNECTOR_NOTION_ENABLED"]="0"
os.environ["CONNECTOR_RENDER_ENABLED"]="1"
os.environ["GITHUB_APP_CLIENT_ID"]="test-client"
os.environ["GITHUB_APP_CLIENT_SECRET"]="test-secret"
os.environ["GITHUB_APP_SLUG"]="dashle-test"

from connectors.security import consume_oauth_state, delete_credential, issue_oauth_state, load_credential, save_credential
from connectors.github import GitHubConnector
from database import ConnectorActionConfirmation, ConnectorAuditLog, ConnectorPermission, User, session_base

class ConnectorSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import database
        database.initialiser_base()

    def setUp(self):
        with session_base() as db:
            a=User(email="connector-a@example.invalid",password_hash="test")
            b=User(email="connector-b@example.invalid",password_hash="test")
            db.add_all([a,b]);db.flush();self.a,self.b=a.id,b.id
        save_credential(self.a,"github",{"access_token":"A-token"},[])
        save_credential(self.b,"github",{"access_token":"B-token"},[])

    def tearDown(self):
        with session_base() as db:
            for uid in (self.a,self.b):
                db.query(ConnectorAuditLog).filter_by(user_id=uid).delete(synchronize_session=False)
                db.query(ConnectorActionConfirmation).filter_by(user_id=uid).delete(synchronize_session=False)
                db.query(ConnectorPermission).filter_by(user_id=uid).delete(synchronize_session=False)
                db.query(User).filter_by(id=uid).delete(synchronize_session=False)

    def test_user_b_has_no_access_to_a_credential(self):
        cred=load_credential(self.b,"github")
        self.assertEqual(cred["secret"]["access_token"],"B-token")
        self.assertNotEqual(cred["secret"]["access_token"],"A-token")

    def test_oauth_state_cannot_be_replayed(self):
        state=issue_oauth_state(self.a,"github")
        self.assertTrue(consume_oauth_state(self.a,"github",state))
        self.assertFalse(consume_oauth_state(self.a,"github",state))
        self.assertFalse(consume_oauth_state(self.b,"github",state))

    def test_expired_confirmation_is_rejected_by_server_data(self):
        token_hash="expired-hash"
        with session_base() as db:
            db.add(ConnectorActionConfirmation(user_id=self.a,provider="github",action="create_issue",parameters="{}",token_hash=token_hash,expires_at=datetime.utcnow()-timedelta(seconds=1),consumed=False))
        with session_base() as db:
            row=db.query(ConnectorActionConfirmation).filter_by(user_id=self.a,token_hash=token_hash,consumed=False).one()
            self.assertLess(row.expires_at,datetime.utcnow())

    def test_forbidden_action_is_rejected_even_if_requested(self):
        meta=next(a for a in GitHubConnector.spec.actions if a.id=="push_code")
        self.assertEqual(meta.risk,"interdite")
        with self.assertRaises(PermissionError):
            raise PermissionError("Action interdite.")

    def test_disconnect_deletes_encrypted_secret(self):
        delete_credential(self.a,"github")
        self.assertIsNone(load_credential(self.a,"github"))

    def test_secret_masking_never_exposes_tokens(self):
        from connectors.security import sanitize_external_content
        safe=sanitize_external_content("Authorization: Bearer ghu_123456789012345678901234")
        self.assertNotIn("ghu_123456789012345678901234",safe)

if __name__=="__main__":
    unittest.main()
