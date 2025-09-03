# app.py — trame factorisée, même UX/comportement
import os
from datetime import datetime, date, time
from flask import Flask, render_template, request, redirect, url_for, flash, session

from models import db, Client, Product, Variant, Movement
from seed import seed_if_empty
import utils as U  # helpers centralisés


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///data.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()
        seed_if_empty()

    # ------------ Filtres Jinja ------------
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

    # ------------ Routes principales ------------
    @app.route("/")
    def index():
        clients = Client.query.order_by(Client.name.asc()).all()
        cards = [U.summarize_client_for_index(c) for c in clients]
        totals = U.summarize_totals(cards)
        return render_template("index.html", cards=cards, totals=totals)

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

    @app.route("/client/<int:client_id>")
    def client_detail(client_id):
        c = Client.query.get_or_404(client_id)
        view = U.summarize_client_detail(c)
        return render_template("client_detail.html", client=c, view=view)

    @app.route("/catalog")
    def catalog():
        # Filtre écocup/gobelet hors catalogue
        variants = (
            db.session.query(Variant)
            .join(Product, Variant.product_id == Product.id)
            .filter(~Product.name.ilike("%ecocup%"), ~Product.name.ilike("%gobelet%"))
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        return render_template("catalog.html", variants=variants)

    # --------- Assistant mouvement (pas-à-pas) ---------
    @app.route("/movement/new", methods=["GET", "POST"])
    def movement_new():
        # On redirige vers l'assistant guide (wizard)
        return redirect(url_for("movement_wizard"))

    @app.route("/movement/wizard", methods=["GET", "POST"])
    def movement_wizard():
        # Wiz = état en session (type, date, client, etc.)
        if "wiz" not in session:
            session["wiz"] = {}
        wiz = session["wiz"]

        if request.method == "GET":
            step = int(request.args.get("step", 1))
            if step == 1:
                # Choix type + date
                return render_template("movement_wizard.html", step=1, wiz=wiz)
            elif step == 2:
                # Choix client
                clients = Client.query.order_by(Client.name.asc()).all()
                return render_template("movement_wizard.html", step=2, wiz=wiz, clients=clients)
            elif step == 3:
                # Récap rapide avant lignes
                return render_template("movement_wizard.html", step=3, wiz=wiz)
            elif step == 4:
                # Lignes (produit/variante/qté/prix/consigne + matériel)
                variants = (
                    db.session.query(Variant)
                    .join(Product, Variant.product_id == Product.id)
                    .filter(~Product.name.ilike("%ecocup%"), ~Product.name.ilike("%gobelet%"))
                    .order_by(Product.name, Variant.size_l)
                    .all()
                )
                return render_template("movement_wizard.html", step=4, wiz=wiz, variants=variants)
            else:
                return redirect(url_for("movement_wizard", step=1))

        # POST
        step = int(request.form.get("step", 1))
        if step == 1:
            # Type, date
            mtype = request.form.get("type")  # OUT/IN/DEFECT/FULL
            if mtype not in U.MOV_TYPES:
                flash("Type invalide.", "warning")
                return redirect(url_for("movement_wizard", step=1))

            wiz["type"] = mtype
            wiz["date"] = request.form.get("date") or None
            session.modified = True
            return redirect(url_for("movement_wizard", step=2))

        elif step == 2:
            # Client
            client_id = request.form.get("client_id", type=int)
            c = Client.query.get(client_id)
            if not c:
                flash("Client introuvable.", "warning")
                return redirect(url_for("movement_wizard", step=2))
            wiz["client_id"] = client_id
            session.modified = True
            return redirect(url_for("movement_wizard", step=3))

        elif step == 3:
            # Confirmation avant saisie lignes
            # (on peut gérer aussi un nb de lignes, mais ici on passe à la table dynamique)
            session.modified = True
            return redirect(url_for("movement_wizard", step=4))

        elif step == 4:
            # Sauvegarde des lignes
            action = request.form.get("action") or "save"
            if action != "save":
                return redirect(url_for("movement_wizard", step=4))

            client_id = wiz.get("client_id")
            if not client_id:
                flash("Client manquant.", "warning")
                return redirect(url_for("movement_wizard", step=2))

            # Date finale
            if wiz.get("date"):
                try:
                    y, m_, d2 = [int(x) for x in wiz["date"].split("-")]
                    created_at = datetime.combine(date(y, m_, d2), time(hour=12))
                except Exception:
                    created_at = U.now_utc()
            else:
                created_at = U.now_utc()

            # Saisie en "table" (listes synchronisées)
            variant_ids = request.form.getlist("variant_id")
            qtys = request.form.getlist("qty")
            unit_prices = request.form.getlist("unit_price_ttc")
            deposits = request.form.getlist("deposit_per_keg")
            notes = request.form.get("notes") or None

            # On supporte aussi la saisie de matériel structuré (form du wizard)
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

                    # Prix par défaut = prix variante si vide
                    if up is None:
                        up = v.price_ttc if v and v.price_ttc is not None else None

                    # >>> CONSIGNE AUTOMATIQUE 30€ SI NON SAISIE <<<
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
                # On réinitialise la table des lignes, mais on garde type/client/date
                return redirect(url_for("client_detail", client_id=client_id))
            else:
                flash("Aucune ligne valide.", "warning")
                return redirect(url_for("movement_wizard", step=4))

        else:
            return redirect(url_for("movement_wizard", step=1))

    @app.route("/movement/<int:movement_id>/delete", methods=["GET", "POST"])
    def movement_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        if request.method == "POST":
            # revert stock
            U.apply_inventory_effect_reverse(m.type, m.variant_id, m.qty or 0)
            db.session.delete(m)
            db.session.commit()
            flash("Mouvement supprimé.", "success")
            return redirect(url_for("client_detail", client_id=m.client_id))
        return render_template("movement_confirm_delete.html", m=m)

    # --------- Pages annexes ---------
    @app.route("/stock")
    def stock():
        rows = U.get_stock_items()
        return render_template("stock.html", rows=rows)

    @app.route("/product/<int:variant_id>")
    def product_variant(variant_id):
        v = Variant.query.get_or_404(variant_id)
        return render_template("product.html", variant=v, product=v.product)

    # --------- Erreurs ---------
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
