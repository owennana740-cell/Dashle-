"""Project attachment validation and extraction limits."""

import unittest

from project_files import MAX_EXTRACTED_TEXT_CHARS, extract_project_file


class ProjectFileProcessingTests(unittest.TestCase):
    def test_text_and_csv_use_verified_types_and_decode_windows_text(self):
        mime, text = extract_project_file("sales.csv", "ville,année\n".encode("cp1252"))
        self.assertEqual(mime, "text/csv")
        self.assertIn("année", text)

        mime, text = extract_project_file("brief.txt", b"Project brief")
        self.assertEqual(mime, "text/plain")
        self.assertEqual(text, "Project brief")

    def test_rejects_binary_text_and_unsupported_formats(self):
        with self.assertRaises(ValueError):
            extract_project_file("secret.exe", b"data")
        with self.assertRaises(ValueError):
            extract_project_file("bad.txt", b"text\x00data")
        with self.assertRaisesRegex(ValueError, "PDF est invalide"):
            extract_project_file("bad.pdf", b"not a pdf")

    def test_extracted_text_is_bounded(self):
        _, text = extract_project_file("large.txt", b"a" * (MAX_EXTRACTED_TEXT_CHARS + 1))
        self.assertEqual(len(text), MAX_EXTRACTED_TEXT_CHARS)


if __name__ == "__main__":
    unittest.main()
