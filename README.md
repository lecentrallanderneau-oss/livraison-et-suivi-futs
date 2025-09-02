# Keg Tracker — MVP (Flask)

MVP ultra simple pour suivre sorties (livraisons) et reprises de fûts chez tes clients,
avec calcul des consignes en jeu.

## Déploiement rapide sur Render

1. Crée **un nouveau dépôt GitHub** et push tout le dossier.
2. Sur **Render**, crée un **Web Service** "from repo".
3. Laisse les valeurs par défaut du `render.yaml` (inclus) :  
   - `SECRET_KEY` généré automatiquement  
   - `DATABASE_URL=sqlite:///data.db`
4. Déploie. La base est **auto-initialisée** au premier démarrage (clients + catalogue).
5. Ouvre l’URL publique depuis ton iPhone.

> **Conseil stockage :** SQLite convient très bien pour démarrer.  
> Pour éviter de perdre la base lors d’un redeploy, active un **disque persistant** sur Render
> *OU* passe sur une base **PostgreSQL managée** et remplace `DATABASE_URL` par l’URL Postgres.

## Utilisation

- **/** : vue d’ensemble (fûts et consignes par client)
- **/clients** : liste des clients
- **/client/<id>** : détail du stock chez le client
- **/movement/new** : formulaire unique Sortie/Reprise (mobile-first)

## Règles catalogue intégrées

- **Coreff Ambrée** ➜ **22 L uniquement**
- **Coreff Rousse** ➜ **20 L uniquement**

*(Ces formats sont imposés par la structure du catalogue. Impossible de saisir autre chose.)*

## Évolutions possibles

- export CSV, auth simple, lieux, etc.
