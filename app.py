# app.py — version simplifiée (même fonctionnalités, code plus clair)
import os
from dataclasses import dataclass
from datetime import datetime, date, time
from typing import Optional, Dict

from flask import Flask, render_template, request, redirect, url_for, flash, session
from sqlalchemy import func

from models import db, Client, Product, Variant, Movement, Inventory, ReorderRule

# ------------------ Constantes ------------------
DEFAULT_DEPOSIT = 30.0  # consigne/fût imposée d'office en livraison si non saisie
MOV_TYPES = {"OUT", "IN", "DEFECT", "FULL"}  # FULL = retour plein non percuté


# ------------------ Dataclasses utilitaires ------------------
@dataclass
class Equipment:
    tireuse: int = 0
    co2: int = 0
    comptoir: int = 0
    tonnelle: int = 0


@dataclass
class Card:
    id: int
    name: str
    kegs: int
    beer_eur: float
    deposit_eur: float
    last_out: Optional[datetime]
    last_in: Optional[datetime]
    equipment: Equipment


# ------------------ Factory ------------------
def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///data.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    with app.app_context():
        db.create_all()
        seed_if_empty()

    # ------------- Helpers de domaine -------------
    def now_utc() -> datetime:
        return datetime.utcnow()

    def parse_equipment(notes: Optional[str]) -> Equipment:
        """
        Extrait le matériel depuis notes, ex :
        'tireuse=1;co2=0;comptoir=0;tonnelle=0' (insensible à la casse).
        Supporte un préfixe éventuel 'EQ|'.
        """
        eq = Equipment()
        if not notes:
            return eq
        part = notes.split("EQ|", 1)[-1] if "EQ|" in notes else notes
        for item in part.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                key = k.strip().lower()
                try:
                    val = int(str(v).strip())
                except Exception:
                    digits = "".join(ch for ch in str(v) if ch.isdigit())
                    val = int(digits) if digits else 0
                if key in ("tireuse", "co2", "comptoir", "tonnelle"):
                    setattr(eq, key, getattr(eq, key) + val)
        return eq

    def effective_price(m: Movement, v: Variant) -> Optional[float]:
        """Prix effectif d'un mouvement : priorité au prix saisi, sinon prix de la variante."""
        return m.unit_price_ttc if m.unit_price_ttc is not None else v.price_ttc

    def effective_deposit(m: Movement) -> float:
        """Consigne effective : valeur saisie sinon 0 (la valeur par défaut est posée à l'enregistrement si OUT)."""
        return m.deposit_per_keg if m.deposit_per_keg is not None else 0.0

    def get_or_create_inventory(variant_id: int) -> Inventory:
        inv = Inventory.query.filter_by(variant_id=variant_id).first()
        if not inv:
            inv = Inventory(variant_id=variant_id, qty=0)
            db.session.add(inv)
            db.session.flush()
        return inv

    def combine_equipment(acc: Equipment, part: Equipment, sign: int):
        """Additionne le matériel avec un signe (+1 ou -1)."""
        acc.tireuse += sign * part.tireuse
        acc.co2 += sign * part.co2
        acc.comptoir += sign * part.comptoir
        acc.tonnelle += sign * part.tonnelle

    def compute_reorder_alerts():
        alerts = []
        rules: Dict[int, ReorderRule] = {r.variant_id: r for r in ReorderRule.query.all()}
        for v in Variant.query.all():
            inv = get_or_create_inventory(v.id)
            rule = rules.get(v.id)
            if rule and rule.min_qty > 0 and (inv.qty or 0) < rule.min_qty:
                alerts.append({
                    "variant": v,
                    "product": v.product,
                    "qty": inv.qty or 0,
                    "min_qty": rule.min_qty,
                    "need": rule.min_qty - (inv.qty or 0),
                })
        return alerts

    # --------- Calculs partagés (index + fiche client) ---------
    def client_movements_full(client_id: int):
        """Retourne la liste des mouvements du client, jointes aux variantes/produits."""
        return (
            db.session.query(Movement, Variant, Product)
            .join(Variant, Movement.variant_id == Variant.id)
            .join(Product, Variant.product_id == Product.id)
            .filter(Movement.client_id == client_id)
            .order_by(Movement.created_at.desc(), Movement.id.desc())
            .all()
        )

    def summarize_client_for_index(c: Client) -> Card:
        """
        Calculs pour la carte d'accueil :
        - kegs en place
        - bière facturée (remboursée si DEFECT/FULL)
        - consignes en jeu
        - matériel net prêté
        - dernières dates OUT/IN
        """
        sums = dict(
            db.session.query(Movement.type, func.coalesce(func.sum(Movement.qty), 0))
            .filter(Movement.client_id == c.id)
            .group_by(Movement.type)
            .all()
        )
        total_out = int(sums.get("OUT", 0))
        total_in = int(sums.get("IN", 0))
        total_def = int(sums.get("DEFECT", 0))
        total_full = int(sums.get("FULL", 0))
        kegs = total_out - (total_in + total_def + total_full)

        beer_eur = 0.0
        deposit_eur = 0.0
        equipment = Equipment()

        for m, v in db.session.query(Movement, Variant)\
                              .join(Variant, Movement.variant_id == Variant.id)\
                              .filter(Movement.client_id == c.id).all():
            price = effective_price(m, v) or 0.0
            dep = effective_deposit(m)
            eq = parse_equipment(m.notes)

            if m.type == "OUT":
                beer_eur += (m.qty or 0) * price
                deposit_eur += (m.qty or 0) * dep
                combine_equipment(equipment, eq, +1)
            elif m.type in {"IN", "DEFECT", "FULL"}:
                deposit_eur -= (m.qty or 0) * dep
                combine_equipment(equipment, eq, -1)
                if m.type in {"DEFECT", "FULL"}:
                    beer_eur -= (m.qty or 0) * price  # remboursement

        last_out = db.session.query(func.max(Movement.created_at))\
            .filter(Movement.client_id == c.id, Movement.type == "OUT").scalar()
        last_in = db.session.query(func.max(Movement.created_at))\
            .filter(Movement.client_id == c.id, Movement.type == "IN").scalar()

        return Card(
            id=c.id, name=c.name, kegs=kegs, beer_eur=beer_eur, deposit_eur=deposit_eur,
            last_out=last_out, last_in=last_in, equipment=equipment
        )

    def summarize_client_for_detail(client_id: int):
        """Calcule les totaux fiche client + la table mouvements formatée."""
        delivered_qty_cum = (
            db.session.query(func.coalesce(func.sum(Movement.qty * Variant.size_l), 0))
            .join(Variant, Movement.variant_id == Variant.id)
            .filter(Movement.client_id == client_id, Movement.type == "OUT")
            .scalar()
        )

        beer_billed_cum = 0.0
        deposit_in_play = 0.0
        equipment_totals = Equipment()
        movements_view = []

        for m, v, p in client_movements_full(client_id):
            price = effective_price(m, v) or 0.0
            dep = effective_deposit(m)
            eq = parse_equipment(m.notes)

            if m.type == "OUT":
                beer_billed_cum += (m.qty or 0) * price
                deposit_in_play += (m.qty or 0) * dep
                combine_equipment(equipment_totals, eq, +1)
            else:
                deposit_in_play -= (m.qty or 0) * dep
                combine_equipment(equipment_totals, eq, -1)
                if m.type in {"DEFECT", "FULL"}:
                    beer_billed_cum -= (m.qty or 0) * price

            movements_view.append(type("MV", (), dict(
                id=m.id,
                created_at=(m.created_at.date().isoformat() if m.created_at else ""),
                type=m.type,
                qty=m.qty,
                unit_price_ttc=m.unit_price_ttc,
                deposit_per_keg=m.deposit_per_keg,
                notes=m.notes,
                variant=v,
                product=p,
            )))

        return delivered_qty_cum or 0, beer_billed_cum or 0.0, deposit_in_play or 0.0, equipment_totals, movements_view

    # ------------- Inventory helpers -------------
    def apply_inventory_effect(mtype: str, variant_id: int, qty: int):
        """Applique l'effet sur le stock bar (OUT baisse, FULL augmente)."""
        if mtype not in MOV_TYPES or qty <= 0:
            return
        if mtype == "OUT":
            inv = get_or_create_inventory(variant_id)
            inv.qty = (inv.qty or 0) - qty
        elif mtype == "FULL":
            inv = get_or_create_inventory(variant_id)
            inv.qty = (inv.qty or 0) + qty

    def revert_inventory_effect(mtype: str, variant_id: int, qty: int):
        """Inverse l'effet lors d'une suppression de mouvement."""
        if mtype == "OUT":
            inv = get_or_create_inventory(variant_id)
            inv.qty = (inv.qty or 0) + qty
        elif mtype == "FULL":
            inv = get_or_create_inventory(variant_id)
            inv.qty = (inv.qty or 0) - qty

    # ------------------ Routes ------------------
    @app.route("/")
    def index():
        cards = [summarize_client_for_index(c) for c in Client.query.order_by(Client.name).all()]
        alerts = compute_reorder_alerts()
        return render_template("index.html", cards=cards, alerts=alerts)

    # ---- Clients CRUD ----
    @app.route("/clients")
    def clients():
        return render_template("clients.html", clients=Client.query.order_by(Client.name).all())

    @app.route("/client/new", methods=["GET", "POST"])
    def client_new():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Nom requis.", "warning")
            else:
                db.session.add(Client(name=name))
                db.session.commit()
                flash("Client ajouté.", "success")
                return redirect(url_for("clients"))
        return render_template("client_form.html", client=None)

    @app.route("/client/<int:client_id>/edit", methods=["GET", "POST"])
    def client_edit(client_id):
        client = Client.query.get_or_404(client_id)
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Nom requis.", "warning")
            else:
                client.name = name
                db.session.commit()
                flash("Client modifié.", "success")
                return redirect(url_for("clients"))
        return render_template("client_form.html", client=client)

    @app.route("/client/<int:client_id>/delete", methods=["POST"])
    def client_delete(client_id):
        c = Client.query.get_or_404(client_id)
        db.session.delete(c)
        db.session.commit()
        flash("Client supprimé.", "success")
        return redirect(url_for("clients"))

    @app.route("/client/<int:client_id>")
    def client_detail(client_id):
        client = Client.query.get_or_404(client_id)
        delivered, beer_cum, deposit_cum, eq_totals, moves_view = summarize_client_for_detail(client.id)
        return render_template(
            "client_detail.html",
            client=client,
            movements=moves_view,
            delivered_qty_cum=delivered,
            beer_billed_cum=beer_cum,
            deposit_in_play=deposit_cum,
            equipment_totals=eq_totals,
        )

    # ---- Mouvement (ancienne page) -> redirige vers l'assistant pas-à-pas
    @app.route("/movement/new", methods=["GET", "POST"])
    def movement_new():
        cid = request.args.get("client_id", type=int)
        return redirect(url_for("movement_wizard", client_id=cid) if cid else url_for("movement_wizard"))

    # ---- Assistant pas-à-pas ----
    @app.route("/movement/wizard", methods=["GET", "POST"])
    def movement_wizard():
        # steps: 1-type, 2-client, 3-date (optionnelle), 4-lignes
        wiz = session.get("wiz", {"step": 1})
        step = int(request.args.get("step", wiz.get("step", 1)))

        # Pré-sélection du client depuis ?client_id=...
        q_cid = request.args.get("client_id", type=int)
        if q_cid and not wiz.get("client_id"):
            wiz["client_id"] = q_cid

        if request.method == "POST":
            action = request.form.get("action", "next")
            if action == "prev":
                step = max(1, step - 1)
            else:
                if step == 1:
                    mtype = request.form.get("type")
                    if mtype not in MOV_TYPES:
                        flash("Choisir Livraison, Reprise, Défectueux ou Retour plein.", "warning")
                    else:
                        wiz["type"] = mtype
                        step = 2
                elif step == 2:
                    client_id = request.form.get("client_id", type=int)
                    if not client_id:
                        flash("Choisir un client.", "warning")
                    else:
                        wiz["client_id"] = client_id
                        step = 3
                elif step == 3:
                    # Date optionnelle : si vide, on utilisera 'now' à l'enregistrement
                    wiz["date"] = (request.form.get("date") or "").strip()
                    step = 4
                elif step == 4:
                    variant_ids = request.form.getlist("variant_id")
                    qtys = request.form.getlist("qty")
                    unit_prices = request.form.getlist("unit_price_ttc")
                    deposits = request.form.getlist("deposit_per_keg")
                    notes = request.form.get("notes") or None

                    # Date finale
                    if wiz.get("date"):
                        try:
                            y, m_, d2 = [int(x) for x in wiz["date"].split("-")]
                            created_at = datetime.combine(date(y, m_, d2), time(hour=12))
                        except Exception:
                            created_at = now_utc()
                    else:
                        created_at = now_utc()

                    client_id = int(wiz["client_id"])
                    mtype = wiz["type"]

                    created = 0
                    for i, vid in enumerate(variant_ids):
                        try:
                            vid_int = int(vid)
                            qty_int = int(qtys[i]) if i < len(qtys) else 0
                            if qty_int <= 0:
                                continue

                            v = Variant.query.get(vid_int)
                            up = float(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else None
                            dep = float(deposits[i]) if i < len(deposits) and deposits[i] else None

                            # En livraison (OUT) : valeurs par défaut imposées si absentes
                            if mtype == "OUT":
                                if up is None:
                                    up = v.price_ttc if v and v.price_ttc is not None else None
                                if dep is None:
                                    dep = DEFAULT_DEPOSIT
                        except Exception:
                            continue

                        mv = Movement(
                            client_id=client_id,
                            variant_id=vid_int,
                            type=mtype,
                            qty=qty_int,
                            unit_price_ttc=up,
                            deposit_per_keg=dep,
                            notes=notes,
                            created_at=created_at,
                        )
                        db.session.add(mv)
                        apply_inventory_effect(mtype, vid_int, qty_int)
                        created += 1

                    db.session.commit()
                    session.pop("wiz", None)
                    flash(f"{created} ligne(s) enregistrée(s).", "success")
                    return redirect(url_for("client_detail", client_id=client_id))

            wiz["step"] = step
            session["wiz"] = wiz

        if step == 1:
            return render_template("movement_wizard.html", step=1, wiz=wiz)
        elif step == 2:
            clients = Client.query.order_by(Client.name).all()
            return render_template("movement_wizard.html", step=2, wiz=wiz, clients=clients)
        elif step == 3:
            return render_template("movement_wizard.html", step=3, wiz=wiz)
        else:
            variants = (
                db.session.query(Variant)
                .join(Product, Variant.product_id == Product.id)
                .order_by(Product.name, Variant.size_l)
                .all()
            )
            return render_template("movement_wizard.html", step=4, wiz=wiz, variants=variants)

    @app.route("/movement/<int:movement_id>/confirm-delete")
    def movement_confirm_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        return render_template("movement_confirm_delete.html", m=m)

    @app.route("/movement/<int:movement_id>/delete", methods=["POST"])
    def movement_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        cid = m.client_id
        revert_inventory_effect(m.type, m.variant_id, m.qty or 0)
        db.session.delete(m)
        db.session.commit()
        flash("Mouvement supprimé.", "success")
        return redirect(url_for("client_detail", client_id=cid))

    # ---- Stock bar (inventaire) ----
    @app.route("/stock", methods=["GET", "POST"])
    def stock():
        if request.method == "POST":
            for v in Variant.query.all():
                val = request.form.get(f"qty_{v.id}")
                if val is None:
                    continue
                try:
                    qty = int(val)
                except Exception:
                    continue
                inv = get_or_create_inventory(v.id)
                inv.qty = qty
            db.session.commit()
            flash("Inventaire mis à jour.", "success")
            return redirect(url_for("stock"))

        variants = (
            db.session.query(Variant)
            .join(Product, Variant.product_id == Product.id)
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        rules_by_vid = {r.variant_id: r for r in ReorderRule.query.all()}
        items = [{"variant": v, "inventory": get_or_create_inventory(v.id), "rule": rules_by_vid.get(v.id)} for v in variants]
        alerts = compute_reorder_alerts()
        return render_template("stock.html", items=items, alerts=alerts)

    # ---- Catalogue (compat) ----
    @app.route("/catalog")
    def catalog():
        rows = (
            db.session.query(Product.name, Variant.size_l, Variant.price_ttc)
            .join(Variant, Variant.product_id == Product.id)
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        return render_template("catalog.html", rows=rows)

    @app.route("/products")
    def products():
        rows = (
            db.session.query(Product.name, Variant.size_l, Variant.price_ttc)
            .join(Variant, Variant.product_id == Product.id)
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        return render_template("product.html", rows=rows)

    # ---- Errors ----
    @app.errorhandler(404)
    def _404(_e):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def _500(_e):
        return render_template("500.html"), 500

    return app


# ------------------ Données par défaut & seed ------------------
DEFAULT_CLIENTS = [
    "Landerneau Football Club",
    "Maison Michel",
    "Ploudiry / Sizun Handball",
]

DEFAULT_CATALOG = [
    ("Coreff Blonde", [20, 30]),
    ("Coreff Blonde Bio", [20]),
    ("Coreff Rousse", [20]),
    ("Coreff Ambrée", [22]),
]

REORDER_DEFAULTS = [
    ("Coreff Blonde", 30, 5),
    ("Coreff Blonde", 20, 2),
]


def seed_if_empty():
    # Clients
    if db.session.query(Client).count() == 0:
        for name in DEFAULT_CLIENTS:
            db.session.add(Client(name=name))
        db.session.commit()

    # Produits + Variantes
    if db.session.query(Product).count() == 0:
        for pname, sizes in DEFAULT_CATALOG:
            p = Product(name=pname)
            db.session.add(p)
            db.session.flush()
            for size in sizes:
                db.session.add(Variant(product_id=p.id, size_l=size, price_ttc=None))
        db.session.commit()

    # Inventaire pour chaque variante
    for v in Variant.query.all():
        if not Inventory.query.filter_by(variant_id=v.id).first():
            db.session.add(Inventory(variant_id=v.id, qty=0))
    db.session.commit()

    # Seuils de réassort par défaut
    for pname, size, minq in REORDER_DEFAULTS:
        v = (
            db.session.query(Variant)
            .join(Product, Variant.product_id == Product.id)
            .filter(Product.name == pname, Variant.size_l == size)
            .first()
        )
        if v:
            r = ReorderRule.query.filter_by(variant_id=v.id).first()
            if not r:
                db.session.add(ReorderRule(variant_id=v.id, min_qty=minq))
            else:
                r.min_qty = minq
    db.session.commit()


# ------------------ Entrée ------------------
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
