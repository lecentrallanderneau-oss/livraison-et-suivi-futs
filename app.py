# app.py — version SANS ecocup/gobelets

import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import func, case
from models import db, Client, Product, Variant, Movement

# --------- Données par défaut ----------
DEFAULT_CLIENTS = [
    "Landerneau Football Club",
    "Maison Michel",
    "Ploudiry / Kermoysan",
    "Association Fest Noz",
    "Comité des Fêtes"
]

DEFAULT_PRODUCTS = [
    ("Coreff Blonde", [20, 30]),
    ("Coreff Ambrée", [20, 30]),
    ("Coreff Blanche", [20]),
    ("Cidre Brut", [20]),
]

# --------- App Factory ----------
def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///data.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key")
    db.init_app(app)

    with app.app_context():
        db.create_all()
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
            func.sum(
                case((Movement.type == "OUT", Movement.quantity), else_=0)
            ).label("out_qty"),
            func.sum(
                case((Movement.type == "IN", Movement.quantity), else_=0)
            ).label("in_qty"),
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

        mv = Movement(
            client_id=client_id,
            variant_id=variant_id,
            type=mtype,
            quantity=qty,
        )
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

    #
