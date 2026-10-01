"""Regression checks for DASHLE public crawlability and sitemap contracts."""

import os
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SESSION_COOKIE_SECURE", "0")

import web


class SeoCrawlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        cls.client = web.app.test_client()

    def test_homepage_is_public_for_browser_and_googlebot(self):
        for user_agent in (
            "Mozilla/5.0",
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        ):
            response = self.client.get("/", headers={"User-Agent": user_agent})
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/html", response.content_type)
            self.assertNotIn("noindex", response.get_data(as_text=True).lower())

    def test_robots_allows_public_pages_and_protects_private_routes(self):
        response = self.client.get(
            "/robots.txt",
            headers={"User-Agent": "Googlebot/2.1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text")
        robots = response.get_data(as_text=True)

        self.assertIn("User-agent: *", robots)
        self.assertIn("Allow: /", robots)
        for public_path in ("/", "/actualites", "/conditions", "/temps-reel", "/tarifs"):
            self.assertNotIn(f"Disallow: {public_path}", robots)
        for private_prefix in ("/connexion", "/inscription", "/parametres", "/securite", "/admin", "/api/"):
            self.assertIn(f"Disallow: {private_prefix}", robots)
        self.assertIn("Sitemap: https://dashle.onrender.com/sitemap.xml", robots)

    def test_sitemap_is_valid_xml_and_contains_only_canonical_public_urls(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")

        root = ET.fromstring(response.get_data())
        namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [node.text for node in root.findall("sm:url/sm:loc", namespace)]

        expected = {
            "https://dashle.onrender.com/",
            "https://dashle.onrender.com/actualites",
            "https://dashle.onrender.com/conditions",
            "https://dashle.onrender.com/temps-reel",
            "https://dashle.onrender.com/tarifs",
        }
        self.assertEqual(set(urls), expected)
        self.assertEqual(len(urls), len(expected))
        for value in urls:
            parsed = urlparse(value)
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.netloc, "dashle.onrender.com")
            self.assertFalse(parsed.query)
            self.assertFalse(parsed.fragment)

    def test_public_sitemap_pages_are_not_redirected_to_login(self):
        for path in ("/", "/actualites", "/conditions", "/temps-reel", "/tarifs"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 200, path)

    def test_private_route_remains_protected(self):
        response = self.client.get("/parametres", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/connexion", response.headers.get("Location", ""))

        response = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
