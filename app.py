import os
from datetime import datetime, date, time
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response

from models import db, Client, Product, Variant, Movement
from seed import seed_if_empty
import utils as U


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///data.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()
        seed_if_empty()

    # ----------------- Healthcheck ultra léger -----------------
    @app.route("/healthz", methods=["GET", "HEAD"])
    def healthz():
        return Response("ok", status=200, mimetype="text/plain")

    # ----------------- Filtres Jinja -----------------
    @app.template_filter("dt")
    def fmt_dt(value):
        if not value:
            return ""
        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            return str(value)

    @app.template_filter("eur")
    def fmt_eur(v):
        if v is None:
            return "-"
        return f"{v:,.2f} €".replace(",", " ").replace(".", ",")

    @app.template_filter("signed_eur")
    def fmt_signed_eur(v):
        if v is None:
            return "-"
        s = "+" if v >= 0 else "−"
        return f"{s}{abs(v):,.2f} €".replace(",", " ").replace(".", ",")

    # ----------------- Routes -----------------
    @app.route("/")
    def index():
        clients = Client.query.order_by(Client.name.asc()).all()
        cards = [U.summarize_client_for_index(c) for c in clients]
        totals = U.summarize_totals(cards)
        alerts = U.compute_reorder_alerts()  # pour _macros.alerts_list
        return render_template("index.html", cards=cards, totals=totals, alerts=alerts)

    @app.route("/clients")
    def clients():
        clients = Client.query.order_by(Client.name.asc()).all()
        return render_template("clients.html", clients=clients)

    @app.route("/client/new", methods=["GET", "POST"])
    def client_new():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("Nom obligatoire.", "warning")
                return render_template("client_form.html")
            c = Client(name=name)
            db.session.add(c)
            db.session.commit()
            flash("Client créé.", "success")
            return redirect(url_for("clients"))
        return render_template("client_form.html")

    # ==== routes attendues par templates ====
    @app.route("/client/<int:client_id>/edit", methods=["GET", "POST"])
    def client_edit(client_id):
        c = Client.query.get_or_404(client_id)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("Nom obligatoire.", "warning")
                return render_template("client_form.html", client=c)
            c.name = name
            db.session.commit()
            flash("Client mis à jour.", "success")
            return redirect(url_for("clients"))
        return render_template("client_form.html", client=c)

    @app.route("/client/<int:client_id>/delete", methods=["POST"])
    def client_delete(client_id):
        c = Client.query.get_or_404(client_id)
        db.session.delete(c)  # cascade sur mouvements OK via models.py
        db.session.commit()
        flash("Client supprimé.", "success")
        return redirect(url_for("clients"))
    # ========================================

    @app.route("/client/<int:client_id>")
    def client_detail(client_id):
        c = Client.query.get_or_404(client_id)
        view = U.summarize_client_detail(c)

        movements = (
            Movement.query.filter_by(client_id=client_id)
            .order_by(Movement.created_at.desc(), Movement.id.desc())
            .all()
        )

        beer_billed_cum = view["beer_eur"]
        deposit_in_play = view["deposit_eur"]
        equipment_totals = view["equipment"]

        return render_template(
            "client_detail.html",
            client=c,
            movements=movements,
            beer_billed_cum=beer_billed_cum,
            deposit_in_play=deposit_in_play,
            equipment_totals=equipment_totals,
        )

    @app.route("/catalog")
    def catalog():
        variants = (
            db.session.query(Variant)
            .join(Product, Variant.product_id == Product.id)
            .filter(~Product.name.ilike("%ecocup%"), ~Product.name.ilike("%gobelet%"))
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        return render_template("catalog.html", variants=variants)

    # ------------- Mouvements -------------
    @app.route("/movement/new", methods=["GET"])
    def movement_new():
        return redirect(url_for("movement_wizard"))

    @app.route("/movement/wizard", methods=["GET", "POST"])
    def movement_wizard():
        if "wiz" not in session:
            session["wiz"] = {}
        wiz = session["wiz"]

        q_client_id = request.args.get("client_id", type=int)
        if q_client_id:
            wiz["client_id"] = q_client_id
            session.modified = True

        if request.method == "GET":
            step = int(request.args.get("step", 1))
            if step == 1:
                return render_template("movement_wizard.html", step=1, wiz=wiz)
            elif step == 2:
                clients = Client.query.order_by(Client.name.asc()).all()
                return render_template("movement_wizard.html", step=2, wiz=wiz, clients=clients)
            elif step == 3:
                return render_template("movement_wizard.html", step=3, wiz=wiz)
            elif step == 4:
                variants = (
                    db.session.query(Variant)
                    .join(Product, Variant.product_id == Product.id)
                    .filter(~Product.name.ilike("%ecocup%"), ~Product.name.ilike("%gobelet%"))
                    .order_by(Product.name, Variant.size_l)
                    .all()
                )
                return render_template("movement_wizard.html", step=4, wiz=wiz, variants=variants)
            return redirect(url_for("movement_wizard", step=1))

        # POST
        step = int(request.form.get("step", 1))
        if step == 1:
            mtype = request.form.get("type")  # 'OUT','IN','DEFECT','FULL'
            if mtype not in U.MOV_TYPES:
                flash("Type invalide.", "warning")
                return redirect(url_for("movement_wizard", step=1))
            wiz["type"] = mtype
            wiz["date"] = request.form.get("date") or None
            session.modified = True
            return redirect(url_for("movement_wizard", step=2))

        elif step == 2:
            client_id = request.form.get("client_id", type=int)
            c = Client.query.get(client_id)
            if not c:
                flash("Client introuvable.", "warning")
                return redirect(url_for("movement_wizard", step=2))
            wiz["client_id"] = client_id
            session.modified = True
            return redirect(url_for("movement_wizard", step=3))

        elif step == 3:
            session.modified = True
            return redirect(url_for("movement_wizard", step=4))

        elif step == 4:
            if (wiz.get("client_id") is None) or (wiz.get("type") is None):
                flash("Informations incomplètes.", "warning")
                return redirect(url_for("movement_wizard", step=1))

            if wiz.get("date"):
                try:
                    y, m_, d2 = [int(x) for x in wiz["date"].split("-")]
                    created_at = datetime.combine(date(y, m_, d2), time(hour=12))
                except Exception:
                    created_at = U.now_utc()
            else:
                created_at = U.now_utc()

            variant_ids = request.form.getlist("variant_id")
            qtys = request.form.getlist("qty")
            unit_prices = request.form.getlist("unit_price_ttc")
            deposits = request.form.getlist("deposit_per_keg")
            notes = request.form.get("notes") or None

            # Matériel structuré (optionnel)
            t = request.form.get("eq_tireuse", type=int)
            c2 = request.form.get("eq_co2", type=int)
            cp = request.form.get("eq_comptoir", type=int)
            tn = request.form.get("eq_tonnelle", type=int)
            if any(v is not None for v in (t, c2, cp, tn)):
                t = t or 0
                c2 = c2 or 0
                cp = cp or 0
                tn = tn or 0
                eq_note = f"tireuse={t};co2={c2};comptoir={cp};tonnelle={tn}"
                notes = f"{(notes + ';') if notes else ''}{eq_note}"

            mtype = wiz["type"]
            client_id = wiz["client_id"]

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

                    if up is None and v and v.price_ttc is not None:
                        up = v.price_ttc

                    # CONSIGNE AUTOMATIQUE 30€ SI NON SAISIE
                    if dep is None:
                        dep = U.DEFAULT_DEPOSIT  # 30.0
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
            if created:
                flash(f"{created} ligne(s) enregistrée(s).", "success")
                return redirect(url_for("client_detail", client_id=client_id))
            else:
                flash("Aucune ligne valide.", "warning")
                return redirect(url_for("movement_wizard", step=4))

        return redirect(url_for("movement_wizard", step=1))

    # ==== suppression mouvement : GET de confirmation + POST de suppression ====
    @app.route("/movement/<int:movement_id>/confirm-delete", methods=["GET"])
    def movement_confirm_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        return render_template("movement_confirm_delete.html", m=m)

    @app.route("/movement/<int:movement_id>/delete", methods=["POST"])
    def movement_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        U.apply_inventory_effect_reverse(m.type, m.variant_id, m.qty or 0)
        client_id = m.client_id
        db.session.delete(m)
        db.session.commit()
        flash("Mouvement supprimé.", "success")
        return redirect(url_for("client_detail", client_id=client_id))
    # ==========================================================================

    @app.route("/stock")
    def stock():
        rows = U.get_stock_items()
        return render_template("stock.html", rows=rows)

    @app.route("/product/<int:variant_id>")
    def product_variant(variant_id):
        v = Variant.query.get_or_404(variant_id)
        return render_template("product.html", variant=v, product=v.product)

    @app.errorhandler(404)
    def not_found(e):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("500.html"), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
