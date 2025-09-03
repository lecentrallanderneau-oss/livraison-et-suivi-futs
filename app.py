import os
from datetime import datetime, date, time
from flask import Flask, render_template, request, redirect, url_for, flash, session
from sqlalchemy import func, case
from models import db, Client, Product, Variant, Movement, Inventory, ReorderRule

# ------------------ Config ------------------
def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///data.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()
        seed_if_empty()

    # -------------- Helpers --------------
    def _price_for_movement(m: Movement, v: Variant):
        """Prix effectif d'un mouvement : priorité au prix saisi, sinon prix variante."""
        if m.unit_price_ttc is not None:
            return m.unit_price_ttc
        return v.price_ttc

    def _deposit_for_movement(m: Movement):
        """Consigne effective : valeur saisie sinon 0 (ou valeur par défaut au moment de l'enregistrement)."""
        return m.deposit_per_keg if m.deposit_per_keg is not None else 0.0

    def parse_equipment(notes: str):
        """
        Extrait le matériel depuis le champ notes, ex :
        'tireuse=1;co2=0;comptoir=0;tonnelle=0' (insensible à la casse).
        Supporte un éventuel préfixe 'EQ|'.
        """
        out = {'tireuse': 0, 'co2': 0, 'comptoir': 0, 'tonnelle': 0}
        if not notes:
            return out
        part = notes.split('EQ|', 1)[-1] if 'EQ|' in notes else notes
        for item in part.split(';'):
            if '=' in item:
                k, v = item.split('=', 1)
                k = k.strip().lower()
                if k in out:
                    try:
                        out[k] = int(str(v).strip())
                    except Exception:
                        digits = ''.join(ch for ch in str(v) if ch.isdigit())
                        out[k] = int(digits) if digits else 0
        return out

    def get_or_create_inventory(variant_id: int) -> Inventory:
        inv = Inventory.query.filter_by(variant_id=variant_id).first()
        if not inv:
            inv = Inventory(variant_id=variant_id, qty=0)
            db.session.add(inv)
            db.session.flush()
        return inv

    def compute_reorder_alerts():
        alerts = []
        rules = {r.variant_id: r for r in ReorderRule.query.all()}
        for v in Variant.query.all():
            inv = get_or_create_inventory(v.id)
            rule = rules.get(v.id)
            if rule and rule.min_qty > 0 and (inv.qty or 0) < rule.min_qty:
                need = rule.min_qty - (inv.qty or 0)
                alerts.append(dict(
                    variant=v, product=v.product, qty=inv.qty or 0, min_qty=rule.min_qty, need=need
                ))
        return alerts

    # -------------- Routes --------------
    @app.route('/')
    def index():
        clients = Client.query.order_by(Client.name).all()
        cards = []

        for c in clients:
            # Compteurs par type
            sums = dict(db.session.query(
                Movement.type, func.coalesce(func.sum(Movement.qty), 0)
            ).filter(Movement.client_id == c.id).group_by(Movement.type).all())

            total_out = int(sums.get('OUT', 0))
            total_in = int(sums.get('IN', 0))
            total_def = int(sums.get('DEFECT', 0))
            total_full = int(sums.get('FULL', 0))

            # Fûts présents chez le client
            kegs = total_out - (total_in + total_def + total_full)

            # Bière facturée (€) : OUT +, DEFECT -, FULL -
            beer_eur = 0.0
            moves = (
                db.session.query(Movement, Variant)
                .join(Variant, Movement.variant_id == Variant.id)
                .filter(Movement.client_id == c.id)
                .all()
            )
            for m, v in moves:
                eff_price = _price_for_movement(m, v) or 0.0
                if m.type == 'OUT':
                    beer_eur += (m.qty or 0) * eff_price
                elif m.type in ('DEFECT', 'FULL'):
                    beer_eur -= (m.qty or 0) * eff_price  # remboursement

            # Consigne en jeu (€) : OUT +, IN/DEFECT/FULL -
            deposit = 0.0
            for m, v in moves:
                dep = _deposit_for_movement(m) or 0.0
                sign = 1 if m.type == 'OUT' else -1
                deposit += sign * dep * (m.qty or 0)

            # Matériel prêté net (OUT +, IN/DEFECT/FULL -)
            equipment_totals = {'tireuse': 0, 'co2': 0, 'comptoir': 0, 'tonnelle': 0}
            for m, v in moves:
                eq = parse_equipment(m.notes)
                sign = 1 if m.type == 'OUT' else -1
                for k in equipment_t_
