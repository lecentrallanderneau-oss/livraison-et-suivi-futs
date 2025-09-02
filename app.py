# app.py — robuste Postgres/SQLite, auto-fix schéma volume_l/deposit_eur, SANS écocup ni pricing
# Compatible Flask 3 (pas de before_first_request)

import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import func, case, text, inspect
from sqlalchemy.engine import Engine
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
    """Normalise DATABASE_URL pour SQLAlchemy/psycopg3 (Render/Heroku)."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def ensure_schema(engine: Engine) -> dict:
    """
    Vérifie/ajoute sur la table 'variant' :
      - volume_l INTEGER
      - deposit_eur INTEGER DEFAULT 0
    Remplit les NULL si besoin.
    """
    insp = inspect(engine)
    tables = insp.get_table_names()
    result = {"added_volume_l": False, "added_deposit_eur": False, "normalized_values": False}

    if "variant" not in tables:
        return result  # créées par create_all()

    cols = {c["name"] for c in insp.get_columns("variant")}

    with engine.connect() as conn:
        if "volume_l" not in cols:
            try:
                conn.execute(text("ALTER TABLE variant ADD COLUMN volume_l INTEGER"))
                result["added_volume_l"] = True
            except Exception:
                pass

        if "deposit_eur" not in cols:
            try:
                conn.execute(text("ALTER TABLE variant ADD COLUMN deposit_eur INTEGER DEFAULT 0"))
                result["added_deposit_eur"] = True
            except Exception:
                pass

        try:
            conn.execute(text("UPDATE variant SET deposit_eur = 0 WHERE deposit_eur IS NULL"))
            conn.execute(text("UPDATE variant SET volume_l = 20 WHERE volume_l IS NULL"))
            result["normalized_values"] = True
        except Exception:
            pass

    return result


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
        # Crée les tables si absentes puis patch le schéma si besoin
        db.create_all()
        ensure_schema(db.engine)

    return app


app = create_app()


# --------- Helpers ----------
def compute_totals():
    """
    Renvoie {(client_id, variant_id): solde_fûts}, avec solde = sum(OUT) - sum(IN)
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
    return {(cid, vid): (out or 0) - (inn or 0) for cid, vid, out, inn in q}


# --------- Routes ----------
@app.route("/")
def index():
    clients = Client.query.order_by(Client.name).all()
    variants = Variant.query.order_by(Variant.id).all()
    totals = compute_totals()
    return render_template("index.html", clients=clients, variants=variants, totals=totals)


@app.route("/catalog")
def catalog():
    products = Product.query.order_by(Product.name).all()
    return render_template("catalog.html", products=products)


@app.route("/movement/new", methods=["GET", "POST"])
def new_movement():
    if request.method == "POST":
        try:
            client_id = int(request.form["client_id"])
            variant_id = int(request.form["variant_id"])
            mtype = request.form["type"]  # "OUT" ou "IN"
            quantity = int(request.form.get("quantity", 1))
        except Exception:
            flash("Formulaire invalide.", "error")
            return redirect(url_for("new_movement"))

        if mtype not in ("OUT", "IN"):
            flash("Type de mouvement invalide.", "error")
            return redirect(url_for("new_movement"))
        if quantity <= 0:
            flash("La quantité doit être positive.", "error")
            return redirect(url_for("new_movement"))

        db.session.add(Movement(client_id=client_id, variant_id=variant_id, type=mtype, quantity=quantity))
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
    client_totals = {k: v for k, v in compute_totals().items() if k[0] == client.id}
    variants = {v.id: v for v in Variant.query.all()}

    return render_template(
        "client_detail.html",
        client=client,
        moves=moves,
        variants=variants,
        client_totals=client_totals,
    )


# --------- Admin/diagnostic ----------
@app.route("/admin/diag")
def admin_diag():
    insp = inspect(db.engine)
    data = {}
    for t in insp.get_table_names():
        cols = insp.get_columns(t)
        data[t] = [{"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True)} for c in cols]
    return jsonify({
        "database_url": normalize_db_url(os.environ.get("DATABASE_URL", "sqlite:///data.db")),
        "tables": data
    })


@app.route("/admin/patch", methods=["GET"])
def admin_patch():
    result = ensure_schema(db.engine)
    return jsonify({"patched": result}), 200


# --------- Command util ----------
@app.cli.command("seed")
def seed_command():
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
