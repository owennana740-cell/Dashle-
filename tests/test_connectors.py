import os
import unittest
from datetime import datetime,timedelta
from connectors.registry import get_connector
from connectors.security import sanitize_external_content

class ConnectorSecurityTests(unittest.TestCase):
    def test_provider_registry_has_required_adapters(self):
        self.assertEqual(get_connector("github").spec.auth_type,"oauth2")
        self.assertEqual(get_connector("google").spec.auth_type,"oauth2")
        self.assertEqual(get_connector("notion").spec.auth_type,"oauth2")
        self.assertEqual(get_connector("render").spec.auth_type,"personal_api_key")

    def test_forbidden_actions_are_declared_forbidden(self):
        github={x.id:x.risk for x in get_connector("github").spec.actions}
        render={x.id:x.risk for x in get_connector("render").spec.actions}
        self.assertEqual(github["push_code"],"interdite")
        self.assertEqual(github["merge_pull_request"],"interdite")
        self.assertEqual(render["delete_service"],"interdite")
        self.assertEqual(render["suspend_service"],"interdite")

    def test_external_content_masks_credentials(self):
        text="Authorization: Bearer abc123 password=secret-value -----BEGIN RSA PRIVATE KEY-----ABC-----END RSA PRIVATE KEY-----"
        cleaned=sanitize_external_content(text)
        self.assertNotIn("abc123",cleaned)
        self.assertNotIn("secret-value",cleaned)
        self.assertNotIn("BEGIN RSA PRIVATE KEY",cleaned)

if __name__=="__main__":
    unittest.main()
