# DASHLE Android

Client Android officiel de DASHLE connecté au backend HTTPS Render.

Fonctionnalités : session HTTPS, chat, inscription/connexion, profil pays+téléphone,
paramètres, tarifs, factures et parcours de paiement existants. Les pages PayDunya et
CinetPay restent accessibles dans le WebView pour conserver le parcours de paiement.

Application ID : com.dashle.app
Minimum : API 26
Target : API 36
Version : 1.0.0

Aucun secret n'est embarqué. La clé de signature Android doit rester hors du dépôt.
Pour Google Play, le livrable release est un Android App Bundle (.aab) signé.