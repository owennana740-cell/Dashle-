"""Regression checks for mobile viewport and safe-area CSS."""

import unittest
from pathlib import Path


class MobileUiCssTests(unittest.TestCase):
    def test_chat_layout_supports_dynamic_mobile_viewport_and_safe_area(self):
        source = Path(__file__).resolve().parents[1].joinpath("web.py").read_text(encoding="utf-8")
        self.assertIn("100dvh", source)
        self.assertIn("env(safe-area-inset-bottom)", source)
        self.assertIn("touch-action: manipulation", source)
        self.assertIn("visualViewport.addEventListener('resize'", source)
        self.assertIn("--dashle-viewport-height", source)

    def test_android_webview_resizes_for_the_soft_keyboard(self):
        manifest = Path(__file__).resolve().parents[1].joinpath(
            "android/app/src/main/AndroidManifest.xml"
        ).read_text(encoding="utf-8")
        self.assertIn('android:windowSoftInputMode="adjustResize"', manifest)


if __name__ == "__main__":
    unittest.main()
