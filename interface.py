import os
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog
from app import traiter_message
from history import charger_conversations, sauvegarder_conversations

try:
    from gtts import gTTS
    import pygame
    pygame.mixer.init()
    VOIX_DISPONIBLE = True
except Exception:
    VOIX_DISPONIBLE = False

def nettoyer_pour_voix(texte):
    symboles = ["*", "#", "_", "`", "~", ">", "•", "-", "[", "]"]
    propre = texte
    for s in symboles:
        propre = propre.replace(s, "")
    return propre

def generer_audio_web(texte):
    """Genere un mp3 dans static/audio pour lecture cote navigateur (sans pygame)."""
    try:
        texte_propre = nettoyer_pour_voix(texte)
        tts = gTTS(text=texte_propre, lang="fr")
        dossier = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "audio")
        os.makedirs(dossier, exist_ok=True)
        chemin = os.path.join(dossier, "dashle_voix.mp3")
        tts.save(chemin)
        return True
    except Exception:
        return False

def parler(texte):
    if not VOIX_DISPONIBLE:
        return
    try:
        texte_propre = nettoyer_pour_voix(texte)
        tts = gTTS(text=texte_propre, lang="fr")
        chemin_temp = os.path.join(tempfile.gettempdir(), "dashle_voix.mp3")
        tts.save(chemin_temp)
        pygame.mixer.music.load(chemin_temp)
        pygame.mixer.music.play()
    except Exception:
        pass

try:
    from PIL import Image
    PIL_DISPONIBLE = True
except Exception:
    PIL_DISPONIBLE = False

# ---------- Couleurs ----------
ACCENT = "#10A37F"
FOND = "#FFFFFF"
SIDEBAR_BG = "#FFFFFF"
BULLE_USER = "#DCF8C6"
BULLE_BOT = "#F1F1F3"
DIM = "#B0B0B0"

def bouton_plat(parent, **kwargs):
    defauts = dict(bd=0, highlightthickness=0, relief=tk.FLAT,
                    activebackground=kwargs.get("bg", FOND))
    defauts.update(kwargs)
    return tk.Button(parent, **defauts)

# ---------- Données ----------
conversations = charger_conversations()
conversation_active = None

def titre_depuis_messages(messages):
    for m in messages:
        if m["auteur"] == "user":
            return m["texte"][:28] + ("…" if len(m["texte"]) > 28 else "")
    return "Nouvelle conversation"

# ---------- Fenêtre principale ----------
fenetre = tk.Tk()
fenetre.title("Dashle")
fenetre.geometry("420x720")
fenetre.configure(bg=FOND)

# ---------- En-tête ----------
entete = tk.Frame(fenetre, bg=ACCENT, height=70)
entete.pack(side=tk.TOP, fill=tk.X)
entete.pack_propagate(False)

def toggle_sidebar():
    if sidebar.winfo_ismapped():
        fermer_sidebar()
    else:
        ouvrir_sidebar()

bouton_plat(entete, text="☰", command=toggle_sidebar, bg=ACCENT, fg="white",
            font=("Segoe UI", 13)).pack(side=tk.LEFT, padx=8)

logo_brut = tk.PhotoImage(file="dashle_logo.png")
logo_img = logo_brut
tk.Label(entete, image=logo_img, bg=ACCENT, bd=0,
         highlightthickness=0).pack(side=tk.LEFT, padx=(0, 4))

tk.Label(entete, text="Dashle", bg=ACCENT, fg="white",
         font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT)

def nouvelle_conversation():
    global conversation_active
    conversation_active = None
    afficher_accueil()
    fermer_sidebar()

bouton_plat(entete, text="＋", command=nouvelle_conversation, bg=ACCENT, fg="white",
            font=("Segoe UI", 14, "bold")).pack(side=tk.RIGHT, padx=8)

# ---------- Corps ----------
corps = tk.Frame(fenetre, bg=FOND)
corps.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

zone_principale = tk.Frame(corps, bg=FOND)
zone_principale.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

voile = tk.Frame(corps, bg=DIM)
sidebar = tk.Frame(corps, bg=SIDEBAR_BG)

def ouvrir_sidebar():
    voile.place(x=0, y=0, relwidth=1, relheight=1)
    voile.lift()
    voile.bind("<Button-1>", lambda e: fermer_sidebar())
    sidebar.place(x=0, y=0, relwidth=0.82, relheight=1)
    sidebar.lift()

def fermer_sidebar():
    voile.place_forget()
    sidebar.place_forget()

tk.Label(sidebar, text="Dashle", bg=SIDEBAR_BG, font=("Segoe UI", 18, "bold"),
          anchor="w").pack(fill=tk.X, padx=18, pady=(20, 16))

bouton_plat(sidebar, text="＋  Nouvelle conversation", anchor="w",
            command=nouvelle_conversation, bg="#F1F1F3",
            font=("Segoe UI", 11), padx=14, pady=10).pack(fill=tk.X, padx=14, pady=(0, 16))

tk.Label(sidebar, text="Récents", bg=SIDEBAR_BG, font=("Segoe UI", 10, "bold"),
          fg="#888", anchor="w").pack(fill=tk.X, padx=18, pady=(0, 4))

liste_conversations = tk.Frame(sidebar, bg=SIDEBAR_BG)
liste_conversations.pack(fill=tk.BOTH, expand=True)

def rafraichir_sidebar():
    for widget in liste_conversations.winfo_children():
        widget.destroy()
    for i, conv in enumerate(conversations):
        bouton_plat(
            liste_conversations, text=conv["titre"], anchor="w",
            bg=SIDEBAR_BG, font=("Segoe UI", 12),
            command=lambda i=i: charger_conversation(i)
        ).pack(fill=tk.X, padx=18, pady=8)

# ---------- Zone de discussion ----------
chat = tk.Text(zone_principale, wrap=tk.WORD, font=("Segoe UI", 11), bg=FOND,
               bd=0, padx=12, pady=8, state=tk.DISABLED)

chat.tag_configure("user", justify="right", background=BULLE_USER,
                    lmargin1=60, lmargin2=60, rmargin=6, spacing1=4, spacing3=10)
chat.tag_configure("bot", justify="left", background=BULLE_BOT,
                    lmargin1=6, rmargin=60, spacing1=4, spacing3=10)

# ---------- Page d'accueil ----------
accueil = tk.Frame(zone_principale, bg=FOND)

accueil_contenu = tk.Frame(accueil, bg=FOND)
accueil_contenu.pack(fill=tk.BOTH, expand=True)

tk.Label(accueil_contenu, text="Bonjour, je suis Dashle", bg=FOND,
          font=("Segoe UI", 16, "bold")).pack(pady=(50, 6))
tk.Label(accueil_contenu, text="Que veux-tu me dire ?", bg=FOND, fg="#666",
          font=("Segoe UI", 11)).pack(pady=(0, 24))

def suggestion(texte):
    champ.delete("1.0", tk.END)
    champ.insert("1.0", texte)
    champ.focus()
    gerer_saisie()

for texte_suggestion in ["Raconte une blague", "Quel est mon nom ?",
                          "apprends que ma couleur préférée = le bleu"]:
    bouton_plat(accueil_contenu, text=texte_suggestion, anchor="w",
                command=lambda t=texte_suggestion: suggestion(t),
                bg="#F1F1F3", font=("Segoe UI", 10),
                padx=10, pady=7).pack(fill=tk.X, padx=26, pady=3)

def verifier_saisie_accueil():
    if conversation_active is None:
        if champ.get("1.0", "end-1c").strip():
            accueil_contenu.pack_forget()
        else:
            accueil_contenu.pack(fill=tk.BOTH, expand=True)

def afficher_accueil():
    chat.pack_forget()
    accueil.pack(fill=tk.BOTH, expand=True)
    accueil_contenu.pack(fill=tk.BOTH, expand=True)

def afficher_chat():
    accueil.pack_forget()
    chat.pack(fill=tk.BOTH, expand=True)

def charger_conversation(i):
    global conversation_active
    conversation_active = i
    chat.config(state=tk.NORMAL)
    chat.delete("1.0", tk.END)
    for m in conversations[i]["messages"]:
        chat.insert(tk.END, m["texte"] + "\n", m["auteur"])
    chat.config(state=tk.DISABLED)
    afficher_chat()
    fermer_sidebar()

# ---------- Indicateur "Dashle réfléchit" : point intégré dans le chat ----------
reflexion_active = False
reflexion_job = None
reflexion_canvas = None
reflexion_debut = None
reflexion_rayon = 8
reflexion_direction = 1

def dessiner_point(rayon):
    reflexion_canvas.delete("all")
    cx, cy = 20, 16
    reflexion_canvas.create_oval(cx - rayon, cy - rayon, cx + rayon, cy + rayon,
                                  fill=ACCENT, outline=ACCENT)

def animer_point():
    global reflexion_rayon, reflexion_direction, reflexion_job
    if not reflexion_active or reflexion_canvas is None:
        return
    reflexion_rayon += reflexion_direction
    if reflexion_rayon >= 13:
        reflexion_direction = -1
    elif reflexion_rayon <= 8:
        reflexion_direction = 1
    dessiner_point(reflexion_rayon)
    reflexion_job = fenetre.after(45, animer_point)

def afficher_reflexion():
    global reflexion_canvas, reflexion_active, reflexion_debut
    global reflexion_rayon, reflexion_direction
    chat.config(state=tk.NORMAL)
    reflexion_debut = chat.index(tk.END)
    reflexion_canvas = tk.Canvas(chat, width=40, height=32, bg=BULLE_BOT,
                                  highlightthickness=0, bd=0)
    chat.window_create(tk.END, window=reflexion_canvas)
    chat.insert(tk.END, "\n", "bot")
    chat.config(state=tk.DISABLED)
    chat.see(tk.END)
    reflexion_rayon = 8
    reflexion_direction = 1
    reflexion_active = True
    animer_point()

def cacher_reflexion():
    global reflexion_active, reflexion_job, reflexion_canvas, reflexion_debut
    reflexion_active = False
    if reflexion_job:
        fenetre.after_cancel(reflexion_job)
        reflexion_job = None
    if reflexion_canvas is not None:
        try:
            reflexion_canvas.destroy()
        except Exception:
            pass
        reflexion_canvas = None
    if reflexion_debut is not None:
        chat.config(state=tk.NORMAL)
        try:
            chat.delete(reflexion_debut, tk.END)
        except Exception:
            pass
        chat.config(state=tk.DISABLED)
        reflexion_debut = None

def joindre_fichier():
    try:
        chemin = filedialog.askopenfilename(
            title="Choisir un fichier",
            filetypes=[
                ("Images et texte", "*.png *.jpg *.jpeg *.gif *.bmp *.txt *.csv *.md"),
                ("Tous les fichiers", "*.*"),
            ]
        )
        if not chemin:
            return

        nom = chemin.split("/")[-1]
        extension = nom.split(".")[-1].lower() if "." in nom else ""

        if extension in ("png", "jpg", "jpeg", "gif", "bmp"):
            if PIL_DISPONIBLE:
                try:
                    with Image.open(chemin) as img:
                        largeur, hauteur = img.size
                        format_img = img.format
                    ajouter_message(
                        f" 🖼 Image jointe : {nom} ({largeur}×{hauteur}, {format_img})",
                        "user"
                    )
                    ajouter_message(
                        "Je vois les informations techniques de cette image, mais je "
                        "ne peux pas encore analyser ce qu'elle montre.",
                        "bot"
                    )
                except Exception:
                    ajouter_message(f" 🖼 Image jointe : {nom}", "user")
            else:
                ajouter_message(f" 🖼 Image jointe : {nom}", "user")

        elif extension in ("txt", "csv", "md"):
            with open(chemin, "r", encoding="utf-8", errors="ignore") as f:
                contenu = f.read()
            apercu = contenu[:800] + ("…" if len(contenu) > 800 else "")
            ajouter_message(f" 📄 Fichier joint : {nom}\n\n{apercu}", "user")
            afficher_reflexion()

            def travail():
                reponse = traiter_message(contenu[:2000])
                fenetre.after(0, lambda: recevoir_reponse(reponse))

            threading.Thread(target=travail, daemon=True).start()

        else:
            ajouter_message(f" 📎 Fichier joint : {nom}", "user")

    except PermissionError:
        ajouter_message(
            "Je n'ai pas accès au stockage. Autorise l'accès pour Pydroid3.",
            "bot"
        )

def ajouter_message(texte, auteur):
    global conversation_active
    if conversation_active is None:
        conversations.insert(0, {"titre": "Nouvelle conversation", "messages": []})
        conversation_active = 0
        afficher_chat()
        chat.config(state=tk.NORMAL)
        chat.delete("1.0", tk.END)
        chat.config(state=tk.DISABLED)

    conversations[conversation_active]["messages"].append({"auteur": auteur, "texte": texte})
    conversations[conversation_active]["titre"] = titre_depuis_messages(
        conversations[conversation_active]["messages"])

    chat.config(state=tk.NORMAL)
    chat.insert(tk.END, texte + "\n", auteur)
    chat.config(state=tk.DISABLED)
    chat.see(tk.END)

    sauvegarder_conversations(conversations)
    rafraichir_sidebar()

# ---------- Barre de saisie ----------
bas = tk.Frame(fenetre, bg=FOND)
bas.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)

bouton_joindre = tk.Canvas(bas, width=34, height=34, bg=FOND,
                            highlightthickness=0, bd=0)
bouton_joindre.create_oval(2, 2, 32, 32, outline="#C7C7C7", width=1.5)
bouton_joindre.create_text(17, 17, text="+", fill="#555555", font=("Segoe UI", 14))
bouton_joindre.bind("<Button-1>", lambda e: joindre_fichier())
bouton_joindre.pack(side=tk.LEFT, padx=(0, 8), pady=2)

bouton_envoyer = tk.Canvas(bas, width=40, height=40, bg=FOND,
                            highlightthickness=0, bd=0)
bouton_envoyer.pack(side=tk.RIGHT, padx=(6, 0), pady=2)
bouton_envoyer.bind("<Button-1>", lambda e: envoyer())

bouton_effacer = tk.Canvas(bas, width=30, height=30, bg=FOND,
                            highlightthickness=0, bd=0)
bouton_effacer.pack(side=tk.RIGHT, padx=(6, 0), pady=2)

def tout_effacer():
    champ.delete("1.0", tk.END)
    gerer_saisie()
    champ.focus()

bouton_effacer.bind("<Button-1>", lambda e: tout_effacer())

champ = tk.Text(bas, font=("Segoe UI", 13), bd=0, relief=tk.FLAT,
                 highlightthickness=0, bg=FOND, height=3, wrap=tk.WORD)
champ.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=2)

fleche_visible = False

def afficher_boutons():
    global fleche_visible
    if not fleche_visible:
        bouton_envoyer.delete("all")
        bouton_envoyer.create_oval(1, 1, 39, 39, fill=ACCENT, outline=ACCENT)
        bouton_envoyer.create_text(20, 19, text="↑", fill="white",
                                    font=("Segoe UI", 13, "bold"))
        bouton_effacer.delete("all")
        bouton_effacer.create_oval(1, 1, 29, 29, outline="#C7C7C7", width=1.5)
        bouton_effacer.create_text(15, 15, text="✕", fill="#777777",
                                    font=("Segoe UI", 10, "bold"))
        fleche_visible = True

def cacher_boutons():
    global fleche_visible
    if fleche_visible:
        bouton_envoyer.delete("all")
        bouton_effacer.delete("all")
        fleche_visible = False

def gerer_saisie(event=None):
    verifier_saisie_accueil()
    if champ.get("1.0", "end-1c").strip():
        afficher_boutons()
    else:
        cacher_boutons()

def recevoir_reponse(reponse):
    cacher_reflexion()
    ajouter_message(reponse, "bot")
    parler(reponse)

def envoyer(event=None):
    texte = champ.get("1.0", "end-1c").strip()
    if not texte:
        return "break"
    champ.delete("1.0", tk.END)
    cacher_boutons()
    ajouter_message(texte, "user")
    verifier_saisie_accueil()
    afficher_reflexion()

    def travail():
        reponse = traiter_message(texte)
        fenetre.after(0, lambda: recevoir_reponse(reponse))

    threading.Thread(target=travail, daemon=True).start()
    return "break"

def gerer_touche_entree(event):
    if event.state & 0x0001:
        fenetre.after(1, gerer_saisie)
        return
    return envoyer()

champ.bind("<KeyRelease>", gerer_saisie)
champ.bind("<Return>", gerer_touche_entree)

# ---------- Démarrage ----------
rafraichir_sidebar()
afficher_accueil()
champ.focus()
fenetre.mainloop()