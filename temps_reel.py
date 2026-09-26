"""Accès aux données de date/heure, météo et actualités à jour."""

from datetime import datetime, timedelta, timezone
from html import unescape
import os
import re
import time
import xml.etree.ElementTree as ET

import requests


FLUX_ACTUALITES = "https://www.lemonde.fr/rss/une.xml"
ATTRIBUTION_ACTUALITES = "Le Monde"
_CACHE_METEO = {}
_CACHE_ACTUALITES = {"expire": 0, "items": []}


def _nettoyer_texte(valeur):
    texte = re.sub(r"<[^>]*>", " ", valeur or "")
    return re.sub(r"\s+", " ", unescape(texte)).strip()


def meteo_du_jour(ville):
    ville = (ville or "").strip()[:80]
    if not ville:
        return {"erreur": "Indique une ville pour afficher la météo."}
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        return {"erreur": "La météo n’est pas configurée sur le serveur."}
    cle_cache = ville.casefold()
    cache = _CACHE_METEO.get(cle_cache)
    if cache and cache[0] > time.time():
        return dict(cache[1])

    base = "https://api.openweathermap.org/data/2.5"
    params = {"q": ville, "appid": api_key, "units": "metric", "lang": "fr"}
    try:
        reponse = requests.get(base + "/weather", params=params, timeout=(3, 5))
        reponse.raise_for_status()
        actuel = reponse.json()
        zone = timezone(timedelta(seconds=int(actuel.get("timezone", 0))))
        date_locale = datetime.fromtimestamp(int(actuel["dt"]), zone).date()
        prevision_reponse = requests.get(base + "/forecast", params=params, timeout=(3, 5))
        prevision_reponse.raise_for_status()
        previsions = prevision_reponse.json().get("list", [])
        jour = [
            point for point in previsions
            if datetime.fromtimestamp(int(point["dt"]), zone).date() == date_locale
        ]
        temperatures = [
            point.get("main", {}).get(cle)
            for point in jour
            for cle in ("temp_min", "temp_max")
            if point.get("main", {}).get(cle) is not None
        ]
        chances_pluie = [float(point.get("pop", 0)) for point in jour]
        details = (actuel.get("weather") or [{}])[0]
        resultat = {
            "ville": actuel.get("name") or ville,
            "pays": (actuel.get("sys") or {}).get("country", ""),
            "date_locale": date_locale.isoformat(),
            "heure_locale": datetime.fromtimestamp(int(actuel["dt"]), zone).strftime("%H:%M"),
            "description": details.get("description", "Conditions indisponibles"),
            "temperature": actuel.get("main", {}).get("temp"),
            "ressenti": actuel.get("main", {}).get("feels_like"),
            "minimum": min(temperatures) if temperatures else actuel.get("main", {}).get("temp_min"),
            "maximum": max(temperatures) if temperatures else actuel.get("main", {}).get("temp_max"),
            "humidite": actuel.get("main", {}).get("humidity"),
            "probabilite_pluie": round(max(chances_pluie) * 100) if chances_pluie else None,
            "source": "OpenWeather",
            "source_url": "https://openweathermap.org/",
        }
        _CACHE_METEO[cle_cache] = (time.time() + 600, resultat)
        return resultat
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return {"erreur": "Le service météo est momentanément indisponible."}


def actualites_recentes(limite=8):
    maintenant = time.time()
    if _CACHE_ACTUALITES["expire"] > maintenant:
        return list(_CACHE_ACTUALITES["items"][:limite])
    try:
        reponse = requests.get(
            FLUX_ACTUALITES,
            headers={"User-Agent": "DASHLE/1.0 (RSS reader)"},
            timeout=(3, 5),
        )
        reponse.raise_for_status()
        racine = ET.fromstring(reponse.content)
        items = []
        for element in racine.iter():
            if not element.tag.lower().endswith("item"):
                continue
            champs = {enfant.tag.split("}")[-1].lower(): _nettoyer_texte(enfant.text or "") for enfant in element}
            titre = champs.get("title", "")
            lien = champs.get("link", "")
            if titre and lien.startswith("https://"):
                items.append({
                    "titre": titre[:240],
                    "url": lien,
                    "date": champs.get("pubdate") or champs.get("published") or "",
                    "source": ATTRIBUTION_ACTUALITES,
                })
            if len(items) >= 20:
                break
        _CACHE_ACTUALITES.update(expire=maintenant + 300, items=items)
        return list(items[:limite])
    except (requests.RequestException, ET.ParseError, ValueError, TypeError):
        return []


def _ville_demandee(message):
    texte = message or ""
    motif = re.search(
        r"\b(?:météo|meteo|temps|weather)\b.{0,50}?\b(?:à|a|de|pour|in|for)\s+([^?!.,;\n]+)",
        texte,
        flags=re.IGNORECASE,
    )
    ville = motif.group(1) if motif else os.environ.get("DASHLE_METEO_VILLE", "")
    ville = re.split(
        r"\b(?:aujourd'hui|aujourd’hui|aujourd hui|demain|maintenant|today|tomorrow|now|ce soir|cette semaine)\b",
        ville,
        flags=re.IGNORECASE,
    )[0]
    return ville.strip(" \t,.-")[:80]


def contexte_temps_reel(message, activites=None):
    texte = (message or "").casefold()
    mots_meteo = ("météo", "meteo", "temps qu'il fait", "weather", "température actuelle")
    mots_actualites = ("actualité", "actualités", "actualite", "actualites", "nouvelles récentes", "news", "latest news")
    mots_horloge = ("quelle heure", "heure actuelle", "quelle date", "date actuelle", "date du jour", "what time", "current date", "current time")
    demande_meteo = any(mot in texte for mot in mots_meteo)
    demande_actualites = any(mot in texte for mot in mots_actualites)
    demande_horloge = any(mot in texte for mot in mots_horloge)
    activites = set(activites or {"meteo", "actualites"})
    demande_meteo = demande_meteo and "meteo" in activites
    demande_actualites = demande_actualites and "actualites" in activites
    if not (demande_meteo or demande_actualites or demande_horloge):
        return ""

    maintenant = datetime.now(timezone.utc)
    blocs = ["Contexte temporel vérifié au moment de cette requête : " + maintenant.strftime("%Y-%m-%d %H:%M UTC") + "."]
    if demande_meteo:
        ville = _ville_demandee(message)
        meteo = meteo_du_jour(ville)
        if meteo.get("erreur"):
            blocs.append(meteo["erreur"] if ville else "Pour la météo, demande une ville précise.")
        else:
            blocs.append(
                "Météo du jour — {ville} ({date_locale}, heure locale {heure_locale}) : {description}; "
                "température {temperature} °C, ressenti {ressenti} °C, minimum prévu {minimum} °C, "
                "maximum prévu {maximum} °C, humidité {humidite} %, probabilité de pluie {probabilite_pluie} %. "
                "Source : OpenWeather.".format(**meteo)
            )
    if demande_actualites:
        actualites = actualites_recentes(5)
        if actualites:
            blocs.append("Titres récents du flux RSS de " + ATTRIBUTION_ACTUALITES + " (titres et liens, pas un résumé intégral) : " + " | ".join(
                item["titre"] + " — " + item["url"] for item in actualites
            ))
        else:
            blocs.append("Le flux d’actualités est momentanément indisponible.")
    return "\n".join(blocs)
