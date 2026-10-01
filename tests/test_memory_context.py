"""Tests du contexte de mémoire utilisateur pertinent."""

import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import brain


class MemoryContextTests(unittest.TestCase):
    def test_relevant_memories_are_selected_without_unrelated_items(self):
        souvenirs = {
            "ville": "Ouagadougou",
            "plat préféré": "riz gras",
            "__dashle_consignes_personnalisees__": "réponds brièvement",
        }
        with patch.object(brain, "memoire_active", return_value=True),                 patch.object(brain, "se_souvenir_tout", return_value=souvenirs):
            contexte = brain._memoire_pertinente("Quel temps fait-il à Ouagadougou ?", user_id=42)

        self.assertIn("ville", contexte)
        self.assertIn("Ouagadougou", contexte)
        self.assertNotIn("riz gras", contexte)
        self.assertNotIn("__dashle_consignes", contexte)

    def test_memory_context_is_disabled_when_personal_memory_is_off(self):
        with patch.object(brain, "memoire_active", return_value=False),                 patch.object(brain, "se_souvenir_tout") as souvenirs:
            self.assertEqual(brain._memoire_pertinente("Où habites-tu ?", user_id=42), "")
            souvenirs.assert_not_called()

    def test_memory_context_has_strict_item_and_character_budgets(self):
        souvenirs = {
            f"préférence {index}": "information personnelle " * 80
            for index in range(12)
        }
        with patch.object(brain, "memoire_active", return_value=True),                 patch.object(brain, "se_souvenir_tout", return_value=souvenirs):
            contexte = brain._memoire_pertinente("mes préférences", user_id=42)

        self.assertLessEqual(len(contexte.splitlines()), brain.MEMOIRE_CONTEXT_MAX_ITEMS)
        self.assertLessEqual(len(contexte), brain.MEMOIRE_CONTEXT_MAX_CHARS)

    def test_system_instruction_contains_selected_memory_as_context(self):
        contexte = "- ville : Ouagadougou"
        instruction = brain._instruction_systeme(
            memoire_pertinente=contexte,
        )
        self.assertIn("Souvenirs personnels pertinents pour ce tour", instruction)
        self.assertIn("Ouagadougou", instruction)


if __name__ == "__main__":
    unittest.main()
