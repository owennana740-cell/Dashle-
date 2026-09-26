"""Analyses statistiques en mémoire des fichiers CSV/XLSX importés."""

from io import BytesIO
from pathlib import Path
import re

import numpy as np
import pandas as pd
from scipy import stats


TAILLE_MAX_LIGNES = 100_000
TAILLE_MAX_COLONNES = 100


def _nom_colonne(nom):
    return str(nom).replace("\n", " ")[:80]


def analyser_fichier(contenu: bytes, nom_fichier: str, question: str) -> tuple[str, dict]:
    extension = Path(nom_fichier or "").suffix.lower()
    if extension == ".csv":
        try:
            cadre = pd.read_csv(BytesIO(contenu), encoding="utf-8-sig", nrows=TAILLE_MAX_LIGNES + 1)
        except UnicodeDecodeError:
            cadre = pd.read_csv(BytesIO(contenu), encoding="cp1252", nrows=TAILLE_MAX_LIGNES + 1)
    elif extension in {".xlsx", ".xlsm", ".xls"}:
        cadre = pd.read_excel(BytesIO(contenu), nrows=TAILLE_MAX_LIGNES + 1)
    else:
        raise ValueError("Envoie un fichier CSV ou Excel (.xls/.xlsx/.xlsm).")

    if cadre.empty:
        raise ValueError("Le fichier ne contient aucune ligne de données.")
    if cadre.shape[0] > TAILLE_MAX_LIGNES or cadre.shape[1] > TAILLE_MAX_COLONNES:
        raise ValueError("Limite du fichier dépassée (100 000 lignes et 100 colonnes maximum).")
    if not len(cadre.columns):
        raise ValueError("Aucune colonne n’a été détectée dans le fichier.")

    cadre.columns = [_nom_colonne(colonne) for colonne in cadre.columns]
    numeriques = cadre.select_dtypes(include=[np.number]).columns.tolist()
    categorielle = cadre.select_dtypes(include=["object", "category", "bool"]).columns.tolist()
    blocs = [
        f"Dimensions : {len(cadre)} lignes × {len(cadre.columns)} colonnes.",
        "Colonnes : " + ", ".join(f"{_nom_colonne(c)} ({cadre[c].dtype})" for c in cadre.columns[:40]),
        "Valeurs manquantes : " + ", ".join(
            f"{_nom_colonne(c)}={int(n)}" for c, n in cadre.isna().sum().items() if n
        ) if cadre.isna().any().any() else "Valeurs manquantes : aucune.",
    ]

    if numeriques:
        description = cadre[numeriques].describe(percentiles=[.25, .5, .75]).round(4)
        blocs.append("Statistiques descriptives (count, mean, std, min, quartiles, max) :\n" + description.to_string())
    for colonne in categorielle[:5]:
        frequences = cadre[colonne].value_counts(dropna=False).head(8)
        effectif = max(1, int(cadre[colonne].notna().sum()))
        blocs.append(f"Fréquences et probabilités empiriques de {_nom_colonne(colonne)} : " + "; ".join(
            f"{str(valeur)[:60]}={int(n)} (p≈{int(n)/effectif:.4f})" for valeur, n in frequences.items()
        ))

    demande = (question or "").casefold()
    if any(mot in demande for mot in ("probabilité", "probabilite", "chance", "probability")) and numeriques:
        condition = re.search(r"(.+?)\s*(>=|<=|>|<|=)\s*(-?\d+(?:[.,]\d+)?)", demande)
        if condition:
            gauche, operateur, seuil_texte = condition.groups()
            colonne = next((c for c in sorted(numeriques, key=len, reverse=True) if c.casefold() in gauche), None)
            if colonne is None and len(numeriques) == 1:
                colonne = numeriques[0]
            if colonne:
                valeurs = pd.to_numeric(cadre[colonne], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
                seuil = float(seuil_texte.replace(",", "."))
                comparaisons = {">": valeurs > seuil, ">=": valeurs >= seuil, "<": valeurs < seuil, "<=": valeurs <= seuil, "=": valeurs == seuil}
                succes = int(comparaisons[operateur].sum())
                n = len(valeurs)
                if n:
                    alpha, beta = succes + 1, n - succes + 1
                    blocs.append(f"Probabilité empirique estimée pour {_nom_colonne(colonne)} {operateur} {seuil:g} : {succes}/{n} = {succes/n:.4f}; intervalle crédible bêta-binomial à 95 % avec a priori Beta(1,1) : [{stats.beta.ppf(.025, alpha, beta):.4f}, {stats.beta.ppf(.975, alpha, beta):.4f}].")
        else:
            blocs.append("Pour estimer une probabilité numérique précise, indique l’événement et son seuil (par exemple : « probabilité que ventes > 100 »). Les fréquences catégorielles ci-dessus sont des estimations empiriques.")
    if any(mot in demande for mot in ("corrélation", "correlation", "pearson", "spearman")) and len(numeriques) >= 2:
        x, y = numeriques[:2]
        paire = cadre[[x, y]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(paire) >= 3:
            pearson = stats.pearsonr(paire[x], paire[y])
            spearman = stats.spearmanr(paire[x], paire[y])
            blocs.append(f"Corrélations entre {_nom_colonne(x)} et {_nom_colonne(y)} (n={len(paire)}) : Pearson r={pearson.statistic:.4f}, p={pearson.pvalue:.4g}; Spearman rho={spearman.statistic:.4f}, p={spearman.pvalue:.4g}.")

    if any(mot in demande for mot in ("régression", "regression", "linéaire", "lineaire")) and len(numeriques) >= 2:
        x, y = numeriques[:2]
        paire = cadre[[x, y]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(paire) >= 3 and paire[x].nunique() > 1:
            resultat = stats.linregress(paire[x], paire[y])
            blocs.append(f"Régression linéaire simple y={_nom_colonne(y)} selon x={_nom_colonne(x)} (n={len(paire)}) : pente={resultat.slope:.5g}, constante={resultat.intercept:.5g}, R²={resultat.rvalue ** 2:.4f}, p={resultat.pvalue:.4g}, erreur-type pente={resultat.stderr:.5g}.")

    if any(mot in demande for mot in ("anova", "analyse de variance")) and numeriques and categorielle:
        valeur, groupe = numeriques[0], categorielle[0]
        groupes = [part[valeur].dropna().to_numpy() for _, part in cadre[[groupe, valeur]].groupby(groupe, observed=True)]
        groupes = [part for part in groupes if len(part) >= 2]
        if len(groupes) >= 2:
            resultat = stats.f_oneway(*groupes)
            blocs.append(f"ANOVA à un facteur sur {_nom_colonne(valeur)} par {_nom_colonne(groupe)} ({len(groupes)} groupes) : F={resultat.statistic:.4g}, p={resultat.pvalue:.4g}. Vérifier l’indépendance, la normalité des résidus et l’homogénéité des variances.")

    if any(mot in demande for mot in ("test t", "t-test", "student", "hypothèse", "hypothese")) and numeriques and categorielle:
        valeur, groupe = numeriques[0], categorielle[0]
        niveaux = cadre[groupe].dropna().unique()
        if len(niveaux) == 2:
            a = cadre.loc[cadre[groupe] == niveaux[0], valeur].dropna().to_numpy()
            b = cadre.loc[cadre[groupe] == niveaux[1], valeur].dropna().to_numpy()
            if len(a) >= 2 and len(b) >= 2:
                resultat = stats.ttest_ind(a, b, equal_var=False, nan_policy="omit")
                blocs.append(f"Test t de Welch sur {_nom_colonne(valeur)} selon {_nom_colonne(groupe)} ({str(niveaux[0])[:40]} n={len(a)}, {str(niveaux[1])[:40]} n={len(b)}) : t={resultat.statistic:.4g}, p={resultat.pvalue:.4g}; différence moyenne={np.mean(a)-np.mean(b):.5g}. Hypothèse bilatérale, variances inégales.")

    if any(mot in demande for mot in ("bayés", "bayes", "bayesian")) and categorielle:
        colonne = categorielle[0]
        niveaux = cadre[colonne].dropna().unique()
        if len(niveaux) == 2:
            succes = int((cadre[colonne] == niveaux[1]).sum())
            echec = int((cadre[colonne] == niveaux[0]).sum())
            alpha, beta = 1 + succes, 1 + echec
            blocs.append(f"Mise à jour bêta-binomiale pour {_nom_colonne(colonne)}, succès défini comme {str(niveaux[1])[:40]} (a priori Beta(1,1)) : {succes} succès, {echec} échecs; moyenne a posteriori={alpha/(alpha+beta):.4f}, intervalle crédible 95 %=[{stats.beta.ppf(.025, alpha, beta):.4f}, {stats.beta.ppf(.975, alpha, beta):.4f}].")

    dates = []
    for colonne in cadre.columns:
        if pd.api.types.is_datetime64_any_dtype(cadre[colonne]):
            dates.append(colonne)
        elif any(mot in _nom_colonne(colonne).casefold() for mot in ("date", "temps", "time", "jour", "mois")):
            convertie = pd.to_datetime(cadre[colonne], errors="coerce", utc=True)
            if convertie.notna().sum() >= max(3, len(cadre) // 2):
                cadre[colonne] = convertie
                dates.append(colonne)
    if any(mot in demande for mot in ("série temporelle", "serie temporelle", "time series", "tendance")) and dates and numeriques:
        date_col, valeur = dates[0], numeriques[0]
        temporel = cadre[[date_col, valeur]].dropna().sort_values(date_col)
        if len(temporel) >= 3 and temporel[date_col].nunique() >= 3:
            x = (temporel[date_col].astype("int64") / 1e9).to_numpy()
            resultat = stats.linregress(x, temporel[valeur].to_numpy())
            blocs.append(f"Tendance temporelle linéaire de {_nom_colonne(valeur)} selon {_nom_colonne(date_col)} (n={len(temporel)}) : variation estimée={resultat.slope * 86400:.5g} unité(s)/jour, R²={resultat.rvalue ** 2:.4f}, p={resultat.pvalue:.4g}. Cette tendance ne modélise pas la saisonnalité.")

    blocs.append("Les tests sont exploratoires : contrôler la qualité des données, les hypothèses et le plan d’échantillonnage avant toute conclusion causale.")
    contexte = "\n\n".join(blocs)
    return contexte[:14000], {"lignes": len(cadre), "colonnes": len(cadre.columns)}
