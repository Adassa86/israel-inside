# Israël Inside — projet prêt pour PyCharm

## Ouvrir le projet

1. Décompresse le fichier ZIP.
2. Dans PyCharm, clique sur **Open**.
3. Choisis le dossier `israel_inside_pyCharm_ready`.
4. Ouvre le terminal de PyCharm.

## Installer

Dans le terminal :

```bash
pip install -r requirements.txt
```

## Lancer

```bash
python app.py
```

Puis ouvre :

- Site public : http://127.0.0.1:5000
- Administration : http://127.0.0.1:5000/admin

## Offre d'exemple

Dans le terminal :

```bash
flask --app app demo
```

Puis actualise la page.

## Base de données

En local, SQLite est créée automatiquement ici :

```text
instance/jobs.db
```

Plus tard, sur Render, l'application peut utiliser PostgreSQL grâce à la variable
d'environnement `DATABASE_URL`.

## Important

La page d'administration n'est pas encore protégée par un mot de passe.
Avant de publier le site sur Internet, il faudra ajouter une authentification.
