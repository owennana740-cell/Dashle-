# DASHLE Android

Projet Android natif isolé du backend Flask. Il utilise Kotlin et Jetpack Compose.
Cette étape pose uniquement la structure et l’écran de démarrage : aucune connexion
au backend ou fonctionnalité de chat n’est encore implémentée.

## Configuration

- Application ID : `com.dashle.app`
- Minimum Android : API 26
- Compilation : API 37
- Cible : API 36
- URL backend par défaut : `https://dashle.onrender.com/`

L’URL, qui est publique et ne contient aucun secret, peut être remplacée par la
propriété Gradle `dashleBaseUrl`, par exemple :

```text
gradle :app:assembleDebug -PdashleBaseUrl=https://serveur-de-test.example/
```

Aucune clé Gemini ni autre secret n’est stocké dans ce projet.
