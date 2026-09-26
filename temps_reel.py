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
            "minimum": min(temperatures) if temperatures else None,
            "maximum": max(temperatures) if temperatures else None,
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
    texte = (message or "").casefold().replace("\u2019", "'")
    mots_meteo = (
        "m\u00e9t\u00e9o", "meteo", "weather", "temps qu'il fait",
        "quel temps fait", "temp\u00e9rature actuelle",
    )
    mots_actualites = (
        "actualit\u00e9", "actualite", "nouvelles r\u00e9centes", "news",
        "latest news", "quoi de neuf", "que se passe-t-il",
        "qu'est-ce qui se passe", "what is happening", "what's happening",
        "what happened",
    )
    mots_horloge = (
        "quelle heure", "heure actuelle", "quelle date", "date actuelle",
        "date du jour", "what time", "current date", "current time",
    )
    demande_meteo = any(mot in texte for mot in mots_meteo)
    demande_actualites = any(mot in texte for mot in mots_actualites)
    demande_horloge = any(mot in texte for mot in mots_horloge)
    marqueur_temps_relatif = re.search(
        r"(?<!\w)(?:maintenant|en ce moment|actuellement|aujourd'hui|"
        r"r\u00e9cemment|recently|today|now|currently|right now|lately|"
        r"ces derniers jours|\u00e0 l'instant)(?!\w)",
        texte,
    )
    if marqueur_temps_relatif and not demande_meteo and not demande_horloge:
        demande_actualites = True

    activites = {"meteo", "actualites"} if activites is None else set(activites)
    demande_meteo = demande_meteo and "meteo" in activites
    demande_actualites = demande_actualites and "actualites" in activites
    if not (demande_meteo or demande_actualites or demande_horloge):
        return ""

    maintenant = datetime.now(timezone.utc)
    blocs = ["Contexte temporel v\u00e9rifi\u00e9 au moment de cette requ\u00eate : " + maintenant.strftime("%Y-%m-%d %H:%M UTC") + "."]
    if demande_meteo:
        ville = _ville_demandee(message)
        meteo = meteo_du_jour(ville)
        if meteo.get("erreur"):
            blocs.append(meteo["erreur"] if ville else "Pour la m\u00e9t\u00e9o, demande une ville pr\u00e9cise.")
        else:
            detail_meteo = (
                "M\u00e9t\u00e9o actuelle \u2014 {ville} ({date_locale}, heure locale {heure_locale}) : "
                "{description}; temp\u00e9rature {temperature} \u00b0C, ressenti {ressenti} \u00b0C."
            ).format(**meteo)
            previsions = []
            if meteo.get("minimum") is not None:
                previsions.append("minimum pr\u00e9vu " + str(meteo["minimum"]) + " \u00b0C")
            if meteo.get("maximum") is not None:
                previsions.append("maximum pr\u00e9vu " + str(meteo["maximum"]) + " \u00b0C")
            if meteo.get("humidite") is not None:
                previsions.append("humidit\u00e9 actuelle " + str(meteo["humidite"]) + " %")
            if meteo.get("probabilite_pluie") is not None:
                previsions.append("probabilit\u00e9 de pluie pr\u00e9vue " + str(meteo["probabilite_pluie"]) + " %")
            if previsions:
                detail_meteo += " Pr\u00e9visions/d\u00e9tails disponibles : " + ", ".join(previsions) + "."
            blocs.append(detail_meteo + " Source : OpenWeather.")
    if demande_actualites:
        actualites = actualites_recentes(5)
        if actualites:
            titres = []
            for item in actualites:
                details = [item["titre"]]
                if item.get("date"):
                    details.append("publi\u00e9 le " + item["date"])
                details.append("source : " + (item.get("source") or ATTRIBUTION_ACTUALITES))
                if item.get("url"):
                    details.append(item["url"])
                titres.append(" \u2014 ".join(details))
            blocs.append(
                "Titres r\u00e9cents du flux RSS de " + ATTRIBUTION_ACTUALITES
                + " (titres et liens originaux, pas un r\u00e9sum\u00e9 int\u00e9gral) : "
                + " | ".join(titres)
            )
        else:
            blocs.append("Le flux d\u2019actualit\u00e9s est momentan\u00e9ment indisponible.")
    return "\n".join(blocs)
