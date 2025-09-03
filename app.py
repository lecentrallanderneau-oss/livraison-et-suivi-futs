import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import func, desc
from models import db, Client, Product, Variant, Movement, Inventory, ReorderRule, EquipmentLoan, EcocupOperation

# ---------------------------------------------------------------------------------
# App & Config (identique à l’existant, seules routes Éco-cups ajoutées)
# ---------------------------------------------------------------------------------
def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///data.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = os.environ.get("SECRET_KEY", "dev")

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _ensure_seed_clients()

    # --------------------------------
    # Routes existantes (accueil, etc)
    # --------------------------------
    @app.route("/")
    def index():
        # cartes existantes (clients, consignes, matériel, etc.) — conservées
        clients = Client.query.order_by(Client.name.asc()).all()

        # alertes réassort (existant)
        low_stock = (
            db.session.query(Variant, Inventory, ReorderRule)
            .join(Inventory, Inventory.variant_id == Variant.id)
            .join(ReorderRule, ReorderRule.variant_id == Variant.id)
            .filter(Inventory.qty < ReorderRule.min_qty)
            .all()
        )

        # NOUVEAU : dernières opérations Éco-cups (pour un visuel direct)
        last_ecocup_ops = (
            EcocupOperation.query.order_by(EcocupOperation.op_date.desc())
            .limit(10)
            .all()
        )

        # NOUVEAU : total Éco-cups à facturer (sur les 30 derniers jours, par exemple)
        last_30_total = (
            db.session.query(func.coalesce(func.sum(EcocupOperation.qty_loaned), 0),
                             func.coalesce(func.sum(EcocupOperation.qty_returned), 0),
                             func.coalesce(func.sum((EcocupOperation.qty_loaned - EcocupOperation.qty_returned) * EcocupOperation.lost_fee_per), 0.0),
                             func.coalesce(func.sum(EcocupOperation.qty_returned * EcocupOperation.wash_fee_per), 0.0))
            .filter(EcocupOperation.op_date >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
            .first()
        )
        day_loaned, day_returned, day_lost_amount, day_wash_amount = last_30_total
        day_total_amount = round((day_lost_amount or 0) + (day_wash_amount or 0), 2)

        return render_template(
            "index.html",
            clients=clients,
            low_stock=low_stock,
            last_ecocup_ops=last_ecocup_ops,            # <-- nouveau
            day_ecocup_summary={
                "loaned": day_loaned or 0,
                "returned": day_returned or 0,
                "lost_amount": round(day_lost_amount or 0, 2),
                "wash_amount": round(day_wash_amount or 0, 2),
                "total": day_total_amount,
            },                                           # <-- nouveau
        )

    # ------------------------------------------------
    # NOUVEAU : Éco-cups — liste + saisie d’une opération
    # ------------------------------------------------
    @app.route("/ecocups", methods=["GET", "POST"])
    def ecocups():
        clients = Client.query.order_by(Client.name.asc()).all()

        if request.method == "POST":
            try:
                client_id = int(request.form.get("client_id") or 0)
                qty_loaned = int(request.form.get("qty_loaned") or 0)
                qty_returned = int(request.form.get("qty_returned") or 0)
                lost_fee_per = float(request.form.get("lost_fee_per") or 1.00)
                wash_fee_per = float(request.form.get("wash_fee_per") or 0.10)
                note = (request.form.get("note") or "").strip()

                if client_id <= 0:
                    flash("Choisis un client.", "warning")
                    return render_template("ecocups.html", clients=clients, ops=_list_ecocup_ops())

                if qty_loaned < 0 or qty_returned < 0:
                    flash("Les quantités ne peuvent pas être négatives.", "warning")
                    return render_template("ecocups.html", clients=clients, ops=_list_ecocup_ops())

                op = EcocupOperation(
                    client_id=client_id,
                    qty_loaned=qty_loaned,
                    qty_returned=qty_returned,
                    lost_fee_per=lost_fee_per,
                    wash_fee_per=wash_fee_per,
                    note=note or None,
                )
                db.session.add(op)
                db.session.commit()

                flash(f"Opération Éco-cups enregistrée : total {op.total_amount:.2f} €", "success")
                return redirect(url_for("ecocups"))

            except Exception as e:
                db.session.rollback()
                flash(f"Erreur lors de l’enregistrement : {e}", "danger")

        return render_template("ecocups.html", clients=clients, ops=_list_ecocup_ops())

    def _list_ecocup_ops(limit: int = 200):
        return (
            EcocupOperation.query.order_by(EcocupOperation.op_date.desc())
            .limit(limit)
            .all()
        )

    # -------------
    # Utilitaires
    # -------------
    def _ensure_seed_clients():
        """
        Assure quelques clients par défaut si la table est vide.
        (Conserve la logique existante, sans toucher aux données en place.)
        """
        if Client.query.count() == 0:
            names = [
                "Landerneau Football Club",
                "Ville de Landerneau",
                "Association des Commerçants",
            ]
            for n in names:
                db.session.add(Client(name=n))
            db.session.commit()

    return app


# -----------------------------------------
# Entrypoint
# -----------------------------------------
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
