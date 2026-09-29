import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"

import web
from brain import OFFRES_DASHLE, _instruction_systeme


class OfferInstructionTests(unittest.TestCase):
    def test_visitor_gets_all_real_offers_and_signup_direction(self):
        prompt = _instruction_systeme(est_visiteur=True)
        self.assertIn("Dashle Free", prompt)
        self.assertIn("Dashle Pro : 15 000 FCFA/mois, 150 000 FCFA/an (2 mois offerts", prompt)
        self.assertIn("Dashle Prime : 25 000 FCFA/mois, 250 000 FCFA/an (2 mois offerts", prompt)
        self.assertIn("Statut de ce chat : visiteur", prompt)
        self.assertIn("page Tarifs", prompt)
        self.assertIn("Créer un compte", prompt)
        self.assertIn("ne dis jamais que Dashle n’en a pas", prompt)

    def test_connected_user_tier_and_tariffs_page_use_same_source(self):
        prompt = _instruction_systeme(niveau="pro", est_visiteur=False)
        self.assertIn("compte connecté, niveau Dashle Pro", prompt)
        client = web.app.test_client()
        with patch.object(web, "_taux_indicatifs", return_value={"eur": 655.957, "usd": .9}):
            response = client.get("/tarifs")
        self.assertEqual(response.status_code, 200)
        self.assertIn(OFFRES_DASHLE["free"]["nom"].encode(), response.data)
        self.assertIn(b"15 000", response.data)
        self.assertIn(b"25 000", response.data)


if __name__ == "__main__":
    unittest.main()
