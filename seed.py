# Optionnel : seed manuel (Render shell -> python seed.py)
from app import app, db, seed_if_empty
with app.app_context():
    seed_if_empty()
    print("Base initialisée ✅")
