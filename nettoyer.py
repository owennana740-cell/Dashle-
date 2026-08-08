import re

with open("conversations.json", "r", encoding="utf-8", errors="ignore") as f:
    contenu = f.read()

# Supprime les caractères de contrôle invalides (retours à la ligne bruts, tabulations, etc.)
contenu_corrige = re.sub(r'[\x00-\x1f]', '', contenu)

with open("conversations.json", "w", encoding="utf-8") as f:
    f.write(contenu_corrige)

print("Fichier nettoyé !")