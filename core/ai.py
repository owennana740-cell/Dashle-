from brain import reflechir

class DashleAI:
    def __init__(self):
        self.nom = "Dashle"
        self.version = "0.1"

    def repondre(self, message):
        return reflechir(message)