"""Régression du contexte sur les conversations longues."""

import os
import unittest

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import brain


class LongConversationContextTests(unittest.TestCase):
    def test_hundred_messages_are_compacted_before_provider_call(self):
        historique = [
            {
                "auteur": "user" if index % 2 == 0 else "bot",
                "texte": f"message-{index} " + ("x" * 900),
            }
            for index in range(100)
        ]

        recent = brain._historique_recent(historique)
        contents = brain._construire_contents("nouveau message", historique)

        self.assertLessEqual(len(recent), brain.MAX_MESSAGES_CONTEXTE)
        self.assertLessEqual(sum(len(str(item["texte"])) for item in recent), 24000)
        self.assertLessEqual(len(contents), brain.MAX_MESSAGES_CONTEXTE + 1)
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"][0]["text"], "nouveau message")

    def test_very_long_single_message_is_bounded(self):
        historique = [{"auteur": "user", "texte": "z" * 100000}]
        recent = brain._historique_recent(historique)

        self.assertEqual(len(recent), 1)
        self.assertEqual(len(recent[0]["texte"]), 24000)


if __name__ == "__main__":
    unittest.main()
