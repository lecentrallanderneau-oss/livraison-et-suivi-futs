# app.py — version SANS écocup + correctifs Postgres/SQLite + auto-fix schéma + diag

import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import func, case, text, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError, OperationalError
from flask_migrate import Migrate

from models import db, Client, Product, Variant, Movement

# --------- Données par défaut ----------
DEFAULT_CLIENTS = [
    "Landerneau Football Club",
    "Maison Michel",
    "Ploudiry / Kermoysan",
    "Association Fest Noz",
    "Comité des Fêtes",
]

DEFAULT_PRODUCTS = [
    ("Coreff Blonde", [20, 30]),
    ("Coreff Ambrée", [20, 30]),
    ("Coreff Blanche", [20]),
    ("Cidre Brut", [20]),
]


def normalize_db_url(url: str) -> str:
    """
    Normalise DATABASE_URL pour SQLAlchemy.
    - Heroku/Render donnent parfois 'postgres://...' -> SQLAlchemy préfère 'postgresql+psycopg://'
    """
    if url.startswith("postgres://"):
        # sqlalchemy 2.x + psycopg3
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        # explicite le driver psycopg si pas présent
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def ensure_schema(engine: Engine):
    """
    Corrige automatiquement le schéma si besoin :
      - Ajoute variant.volume_l (INTEGER)
      - Ajoute variant.deposit_eur (INTEGER DEFAULT 0)
    Compatible PostgreSQL / SQLite.
    """
    insp = inspect(engine)
    existing_tables = insp.get_table_names()
    if "variant" not in existing_tables:
        # Rien à faire ici : db.create_all() s’en chargera avant.
        return

    # Liste des colonnes existantes
    existing_cols = {col["name"] for col in insp.get_columns("variant")}

    with engine.connect() as conn:
        # Ajout de volume_l si manquant
        if "volume_l" not in existing_cols:
            try:
                conn.execute(text("ALTER TABLE variant ADD COLUMN volume_l INTEGER"))
            except Exception:
                pass  # si la colonne existe déjà suite à race condition

        # Ajout de deposit_eur si manquant
        if "deposit_eur" not in existing_cols:
            try:
                conn.execute(text("ALTER TABLE variant ADD COLUMN deposit_eur INTEGER DEFAULT 0"))
            except Exception:
                pass

        # Normaliser valeurs NULL
        try:
            conn.execute(text("UPDATE variant SET deposit_eur = 0 WHERE deposit_eur IS NULL"))
        except Exception:
            pass
        try:
            conn.execute(text("UPDATE variant SET volume_l = 20 WHERE volume_l IS NULL"))
        except Exception:
            pass


# --------- App Factory ----------
def create_app():
    app = Flask(__name__)

    raw_url = os.environ.get("DATABASE_URL", "sqlite:///data.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = normalize_db_url(raw_url)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key")

    db.init_app(app)
    Migrate(app, db)

    with app.app_context():
        # Création des tables (si absentes)
        db.create_all()
        # Patch schéma si colonnes manquantes
        ensure_schema(db.engine)

    return app


app = create_app()


# --------- Helpers ----------
def compute_totals():
    """
    Renvoie un dict {(client_id, variant_id): solde_futs}
    solde = sum(OUT) - sum(IN)
    """
    q = (
        db.session.query(
            Movement.client_id,
            Movement.variant_id,
            func.sum(case((Movement.type == "OUT", Movement.quantity), else_=0)).label("out_qty"),
            func.sum(case((Movement.type == "IN", Movement.quantity), else_=0)).label("in_qty"),
        )
        .group_by(Movement.client_id, Movement.variant_id)
        .all()
    )

    totals = {}
    for client_id, variant_id, out_qty, in_qty in q:
        out_qty = out_qty or 0
        in_qty = in_qty or 0
        totals[(client_id, variant_id)] = out_qty - in_qty
    return totals


# --------- Routes ----------
@app.route("/")
def index():
    clients = Client.query.order_by(Client.name).all()
    variants = Variant.query.order_by(Variant.id).all()
    totals = compute_totals()
    return render_template("index.html", clients=clients, variants=variants, totals=totals)


@app.route("/catalog")
def catalog():
    """Catalogue des produits / variantes disponibles (affichage simple)."""
    products = Product.query.order_by(Product.name).all()
    return render_template("catalog.html", products=products)


@app.route("/movement/new", methods=["GET", "POST"])
def new_movement():
    if request.method == "POST":
        try:
            client_id = int(request.form["client_id"])
            variant_id = int(request.form["variant_id"])
            mtype = request.form["type"]  # "OUT" ou "IN"
            qty = int(request.form.get("quantity", 1))
        except Exception:
            flash("Formulaire invalide.", "error")
            return redirect(url_for("new_movement"))

        if mtype not in ("OUT", "IN"):
            flash("Type de mouvement invalide.", "error")
            return redirect(url_for("new_movement"))

        if qty <= 0:
            flash("La quantité doit être positive.", "error")
            return redirect(url_for("new_movement"))

        mv = Movement(client_id=client_id, variant_id=variant_id, type=mtype, quantity=qty)
        db.session.add(mv)
        db.session.commit()
        flash("Mouvement enregistré.", "success")
        return redirect(url_for("index"))

    clients = Client.query.order_by(Client.name).all()
    variants = Variant.query.order_by(Variant.id).all()
    return render_template("movement_form.html", clients=clients, variants=variants)


@app.route("/client/<int:client_id>")
def client_detail(client_id):
    client = Client.query.get_or_404(client_id)

    moves = (
        Movement.query.filter_by(client_id=client.id)
        .order_by(Movement.created_at.desc(), Movement.id.desc())
        .all()
    )

    totals_all = compute_totals()
    client_totals = {(cid, vid): qty for (cid, vid), qty in totals_all.items() if cid == client.id}

    variants = {v.id: v for v in Variant.query.all()}

    return render_template(
        "client_detail.html",
        client=client,
        moves=moves,
        variants=variants,
        client_totals=client_totals,
    )


# --------- Admin/diagnostic (utile en prod) ----------
@app.route("/admin/diag")
def admin_diag():
    """
    Retourne un aperçu du schéma courant : tables + colonnes.
    Pratique pour vérifier en prod que volume_l et deposit_eur existent.
    """
    insp = inspect(db.engine)
    data = {}
    for t in insp.get_table_names():
        cols = insp.get_columns(t)
        data[t] = [{"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True)} for c in cols]
    return jsonify({"url": normalize_db_url(os.environ.get("DATABASE_URL", "sqlite:///data.db")), "schema": data})


# --------- Command util ----------
@app.cli.command("seed")
def seed_command():
    """
    flask seed
    Remplit la base avec quelques clients / produits / variantes.
    """
    if not Client.query.first():
        for name in DEFAULT_CLIENTS:
            db.session.add(Client(name=name))
    if not Product.query.first():
        for pname, vols in DEFAULT_PRODUCTS:
            p = Product(name=pname)
            db.session.add(p)
            db.session.flush()
            for vol in vols:
                db.session.add(Variant(product_id=p.id, volume_l=vol, deposit_eur=0))
    db.session.commit()
    print("Base peuplée (clients / produits / variantes).")


if __name__ == "__main__":
    # Démarrage local : python app.py
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
