# app.py — trame factorisée, même UX/comportement
import os
from datetime import datetime, date, time
from flask import Flask, render_template, request, redirect, url_for, flash, session

from models import db, Client, Product, Variant, Movement
from seed import seed_if_empty
import utils as U  # helpers centralisés

def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///data.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    with app.app_context():
        db.create_all()
        seed_if_empty()

    # ---------------- Routes ----------------
    @app.route("/")
    def index():
        cards = U.summarize_all_clients()
        alerts = U.compute_reorder_alerts()
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
        delivered, beer_cum, deposit_cum, eq_totals, moves_view = U.summarize_client_for_detail(client.id)
        return render_template(
            "client_detail.html",
            client=client,
            movements=moves_view,
            delivered_qty_cum=delivered,
            beer_billed_cum=beer_cum,
            deposit_in_play=deposit_cum,
            equipment_totals=eq_totals,
        )

    # ---- Mouvement (ancienne page) -> assistant
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
                    if mtype not in U.MOV_TYPES:
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
                            created_at = U.now_utc()
                    else:
                        created_at = U.now_utc()

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

                            # En livraison (OUT) : défauts imposés si absents
                            if mtype == "OUT":
                                if up is None:
                                    up = v.price_ttc if v and v.price_ttc is not None else None
                                if dep is None:
                                    dep = U.DEFAULT_DEPOSIT
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
                        U.apply_inventory_effect(mtype, vid_int, qty_int)
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
        U.revert_inventory_effect(m.type, m.variant_id, m.qty or 0)
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
                inv = U.get_or_create_inventory(v.id)
                inv.qty = qty
            db.session.commit()
            flash("Inventaire mis à jour.", "success")
            return redirect(url_for("stock"))

        items = U.get_stock_items()
        alerts = U.compute_reorder_alerts()
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


# Entrée
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
