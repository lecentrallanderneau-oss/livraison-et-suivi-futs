import os
from datetime import datetime, date, time
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from sqlalchemy import func

from models import db, Client, Product, Variant, Movement, ReorderRule
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

    # ----------------- Healthcheck -----------------
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

    # ----------------- Helper: fûts “ouverts” par variante chez un client -----------------
    def _open_kegs_by_variant(client_id: int):
        out_rows = dict(
            db.session.query(Movement.variant_id, func.coalesce(func.sum(Movement.qty), 0))
            .filter(Movement.client_id == client_id, Movement.type == "OUT")
            .group_by(Movement.variant_id)
            .all()
        )
        back_rows = dict(
            db.session.query(Movement.variant_id, func.coalesce(func.sum(Movement.qty), 0))
            .filter(Movement.client_id == client_id, Movement.type.in_(["IN", "DEFECT", "FULL"]))
            .group_by(Movement.variant_id)
            .all()
        )
        all_vids = set(out_rows) | set(back_rows)
        return {vid: int(out_rows.get(vid, 0)) - int(back_rows.get(vid, 0)) for vid in all_vids}

    # ----------------- Pages -----------------
    @app.route("/")
    def index():
        clients = Client.query.order_by(Client.name.asc()).all()
        cards = [U.summarize_client_for_index(c) for c in clients]
        totals = U.summarize_totals(cards)
        alerts = U.compute_reorder_alerts()
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
        db.session.delete(c)
        db.session.commit()
        flash("Client supprimé.", "success")
        return redirect(url_for("clients"))

    @app.route("/client/<int:client_id>")
    def client_detail(client_id):
        c = Client.query.get_or_404(client_id)
        view = U.summarize_client_detail(c)
        movements = (
            Movement.query.filter_by(client_id=client_id)
            .order_by(Movement.created_at.desc(), Movement.id.desc())
            .all()
        )
        return render_template(
            "client_detail.html",
            client=c,
            view=view,
            movements=movements,
            beer_billed_cum=view["beer_eur"],
            deposit_in_play=view["deposit_eur"],
            equipment_totals=view["equipment"],
            liters_out_cum=view.get("liters_out_cum", 0.0),
            litres_out_cum=view.get("liters_out_cum", 0.0),
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

    # ------------- Saisie (ex “mouvement pas à pas”) -------------
    @app.route("/movement/new", methods=["GET"])
    def movement_new():
        return redirect(url_for("movement_wizard"))

    @app.route("/movement/wizard", methods=["GET", "POST"])
    def movement_wizard():
        if "wiz" not in session:
            session["wiz"] = {}
        wiz = session["wiz"]

        # Préselection client éventuelle
        q_client_id = request.args.get("client_id", type=int)
        if q_client_id:
            wiz["client_id"] = q_client_id
            session.modified = True

        if request.method == "GET":
            step = int(request.args.get("step", 1))
            if step == 1:
                # Date NON pré-remplie ; si vide à l’enregistrement -> date de saisie
                return render_template("movement_wizard.html", step=1, wiz=wiz)
            elif step == 2:
                clients = Client.query.order_by(Client.name.asc()).all()
                return render_template("movement_wizard.html", step=2, wiz=wiz, clients=clients)
            elif step == 3:
                current_client = Client.query.get(wiz["client_id"]) if wiz.get("client_id") else None
                return render_template("movement_wizard.html", step=3, wiz=wiz, current_client=current_client)
            elif step == 4:
                # Base: tout sauf ecocup/gobelet
                base_q = (
                    db.session.query(Variant)
                    .join(Product, Variant.product_id == Product.id)
                    .filter(~Product.name.ilike("%ecocup%"), ~Product.name.ilike("%gobelet%"))
                    .order_by(Product.name, Variant.size_l)
                )

                # En Reprise (IN) : limiter aux fûts “en jeu” + TOUJOURS ajouter “Matériel seul …”
                if wiz.get("type") == "IN" and wiz.get("client_id"):
                    open_map = _open_kegs_by_variant(wiz["client_id"])
                    allowed_ids = {vid for vid, openq in open_map.items() if openq > 0}

                    equip_ids = set(
                        vid for (vid,) in (
                            db.session.query(Variant.id)
                            .join(Product, Variant.product_id == Product.id)
                            .filter(
                                (~Product.name.ilike("%ecocup%")),
                                (~Product.name.ilike("%gobelet%")),
                                (
                                    Product.name.ilike("%matériel%seul%")
                                    | Product.name.ilike("%materiel%seul%")
                                    | Product.name.ilike("%Matériel seul%")
                                    | Product.name.ilike("%Materiel seul%")
                                )
                            )
                            .all()
                        )
                    )

                    final_ids = list(allowed_ids | equip_ids)
                    if final_ids:
                        base_q = base_q.filter(Variant.id.in_(final_ids))
                    else:
                        base_q = base_q.filter(Variant.id.in_([-1]))
                        flash("Aucune référence disponible à la reprise pour ce client.", "info")

                variants = base_q.all()
                return render_template("movement_wizard.html", step=4, wiz=wiz, variants=variants)
            return redirect(url_for("movement_wizard", step=1))

        # POST
        step = int(request.form.get("step", 1))
        if step == 1:
            mtype = request.form.get("type")
            if mtype not in U.MOV_TYPES:
                flash("Type invalide.", "warning")
                return redirect(url_for("movement_wizard", step=1))
            wiz["type"] = mtype
            wiz["date"] = request.form.get("date") or None  # peut rester vide -> date de saisie
            session.modified = True
            return redirect(url_for("movement_wizard", step=2))

        elif step == 2:
            client_id = request.form.get("client_id", type=int)
            if not Client.query.get(client_id):
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

            # Date finale : vide -> date de saisie (now)
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

            # Matériel prêté/repris (inséré en note)
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

            open_map = _open_kegs_by_variant(client_id) if mtype == "IN" else {}
            created = 0
            violations = []

            for i, vid in enumerate(variant_ids):
                try:
                    vid_int = int(vid)
                except Exception:
                    continue

                try:
                    qty_int = int(qtys[i]) if i < len(qtys) else 0
                except Exception:
                    qty_int = 0

                up = None
                dep = None
                if i < len(unit_prices) and unit_prices[i]:
                    try:
                        up = float(unit_prices[i])
                    except Exception:
                        up = None
                if i < len(deposits) and deposits[i]:
                    try:
                        dep = float(deposits[i])
                    except Exception:
                        dep = None

                v = Variant.query.get(vid_int)
                if not v:
                    continue

                # “Matériel seul” => forcer 0 partout et PAS de contrôle d’enjeu de fûts
                pname = (v.product.name if v and v.product else "") or ""
                is_equipment_only = "matériel" in pname.lower() and "seul" in pname.lower()
                if is_equipment_only:
                    qty_int = 0
                    up = 0.0
                    dep = 0.0
                else:
                    if up is None and (v.price_ttc is not None):
                        up = v.price_ttc
                    if dep is None:
                        dep = U.DEFAULT_DEPOSIT  # 30€

                    if mtype == "IN":
                        open_q = int(open_map.get(vid_int, 0))
                        if open_q <= 0 or qty_int > open_q:
                            label = f"{v.product.name} — {v.size_l} L"
                            violations.append((label, open_q))
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

                if qty_int > 0:
                    U.apply_inventory_effect(mtype, vid_int, qty_int)
                created += 1

            if violations:
                db.session.rollback()
                msgs = [f"{label} (max {maxq})" for (label, maxq) in violations]
                flash("Reprise impossible pour : " + ", ".join(msgs), "warning")
                return redirect(url_for("movement_wizard", step=4))

            db.session.commit()
            if created:
                flash(f"{created} ligne(s) enregistrée(s).", "success")
                return redirect(url_for("client_detail", client_id=client_id))
            else:
                flash("Aucune ligne valide.", "warning")
                return redirect(url_for("movement_wizard", step=4))

        return redirect(url_for("movement_wizard", step=1))

    # ---- suppression mouvement ----
    @app.route("/movement/<int:movement_id>/confirm-delete", methods=["GET"])
    def movement_confirm_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        return render_template("movement_confirm_delete.html", m=m)

    @app.route("/movement/<int:movement_id>/delete", methods=["POST"])
    def movement_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        client_id = m.client_id  # sauvegarde avant suppression

        # RÉTABLIR LE STOCK ICI (au lieu d'appeler utils.revert_inventory_effect)
        # - OUT   : on remet +qty en stock
        # - FULL  : on retire qty du stock (retour plein annulé)
        # - IN/DEFECT : pas d’impact sur le stock de fûts pleins
        if m.qty and m.variant_id:
            inv = U.get_or_create_inventory(m.variant_id)
            if m.type == "OUT":
                inv.qty = (inv.qty or 0) + (m.qty or 0)
            elif m.type == "FULL":
                inv.qty = (inv.qty or 0) - (m.qty or 0)

        db.session.delete(m)
        db.session.commit()
        flash("Saisie supprimée.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    # ---- Stock ----
    @app.route("/stock", methods=["GET", "POST"])
    def stock():
        if request.method == "POST":
            changed = 0

            # QTY_*  -> Inventory
            for key, val in request.form.items():
                if not key.startswith("qty_"):
                    continue
                try:
                    vid = int(key.split("_", 1)[1])
                except Exception:
                    continue
                try:
                    qty = int(val or 0)
                except Exception:
                    qty = 0
                inv = U.get_or_create_inventory(vid)
                if inv.qty != qty:
                    inv.qty = qty
                    changed += 1

            # MIN_* -> ReorderRule
            for key, val in request.form.items():
                if not key.startswith("min_"):
                    continue
                try:
                    vid = int(key.split("_", 1)[1])
                except Exception:
                    continue
                try:
                    minq = int(val or 0)
                except Exception:
                    minq = 0

                rule = ReorderRule.query.filter_by(variant_id=vid).first()
                if not rule:
                    rule = ReorderRule(variant_id=vid, min_qty=minq)
                    db.session.add(rule)
                    changed += 1
                else:
                    if rule.min_qty != minq:
                        rule.min_qty = minq
                        changed += 1

            db.session.commit()
            flash(f"Inventaire enregistré ({changed} mise(s) à jour).", "success")
            return redirect(url_for("stock"))

        rows = U.get_stock_items()
        alerts = U.compute_reorder_alerts()
        return render_template("stock.html", rows=rows, alerts=alerts)

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
