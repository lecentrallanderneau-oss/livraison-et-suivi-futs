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

            last_out = db.session.query(func.max(Movement.created_at))\
                .filter(Movement.client_id == c.id, Movement.type == 'OUT').scalar()
            last_in = db.session.query(func.max(Movement.created_at))\
                .filter(Movement.client_id == c.id, Movement.type == 'IN').scalar()

            cards.append(type('Card', (), dict(
                id=c.id, name=c.name, kegs=kegs, beer_eur=beer_eur, deposit_eur=deposit,
                last_out=last_out, last_in=last_in
            )))

        alerts = compute_reorder_alerts()
        return render_template('index.html', cards=cards, alerts=alerts)

    # ---- Clients CRUD ----
    @app.route('/clients')
    def clients():
        clients = Client.query.order_by(Client.name).all()
        return render_template('clients.html', clients=clients)

    @app.route('/client/new', methods=['GET', 'POST'])
    def client_new():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name:
                flash("Nom requis.", "warning")
            else:
                db.session.add(Client(name=name))
                db.session.commit()
                flash("Client ajouté.", "success")
                return redirect(url_for('clients'))
        return render_template('client_form.html', client=None)

    @app.route('/client/<int:client_id>/edit', methods=['GET', 'POST'])
    def client_edit(client_id):
        client = Client.query.get_or_404(client_id)
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name:
                flash("Nom requis.", "warning")
            else:
                client.name = name
                db.session.commit()
                flash("Client modifié.", "success")
                return redirect(url_for('clients'))
        return render_template('client_form.html', client=client)

    @app.route('/client/<int:client_id>/delete', methods=['POST'])
    def client_delete(client_id):
        c = Client.query.get_or_404(client_id)
        db.session.delete(c)
        db.session.commit()
        flash("Client supprimé.", "success")
        return redirect(url_for('clients'))

    @app.route('/client/<int:client_id>')
    def client_detail(client_id):
        client = Client.query.get_or_404(client_id)

        # Totaux livrés (litres), bière facturée cumulée, consignes en jeu
        delivered_qty_cum = db.session.query(
            func.coalesce(func.sum(Movement.qty * Variant.size_l), 0)
        ).join(Variant, Movement.variant_id == Variant.id)\
         .filter(Movement.client_id == client.id, Movement.type == 'OUT')\
         .scalar()

        beer_billed_cum = 0.0
        deposit_in_play = 0.0

        moves = (
            db.session.query(Movement, Variant, Product)
            .join(Variant, Movement.variant_id == Variant.id)
            .join(Product, Variant.product_id == Product.id)
            .filter(Movement.client_id == client.id)
            .order_by(Movement.created_at.desc(), Movement.id.desc())
            .all()
        )

        movements_view = []
        for m, v, p in moves:
            eff_price = _price_for_movement(m, v) or 0.0
            dep = _deposit_for_movement(m) or 0.0

            if m.type == 'OUT':
                beer_billed_cum += (m.qty or 0) * eff_price
                deposit_in_play += (m.qty or 0) * dep
            elif m.type in ('IN', 'DEFECT', 'FULL'):
                deposit_in_play -= (m.qty or 0) * dep
                if m.type in ('DEFECT', 'FULL'):
                    beer_billed_cum -= (m.qty or 0) * eff_price  # remboursement

            movements_view.append(type('MV', (), dict(
                id=m.id,
                created_at=(m.created_at.date().isoformat() if m.created_at else ''),
                type=m.type,
                qty=m.qty,
                unit_price_ttc=m.unit_price_ttc,
                deposit_per_keg=m.deposit_per_keg,
                notes=m.notes,
                variant=v,
                product=p,
            )))

        return render_template(
            'client_detail.html',
            client=client,
            movements=movements_view,
            delivered_qty_cum=delivered_qty_cum or 0,
            beer_billed_cum=beer_billed_cum or 0.0,
            deposit_in_play=deposit_in_play or 0.0,
        )

    # ---- Ancienne page -> redirige vers l'assistant pas-à-pas
    @app.route('/movement/new', methods=['GET', 'POST'])
    def movement_new():
        cid = request.args.get('client_id', type=int)
        return redirect(url_for('movement_wizard', client_id=cid) if cid else url_for('movement_wizard'))

    # ---- Assistant pas-à-pas ----
    @app.route('/movement/wizard', methods=['GET', 'POST'])
    def movement_wizard():
        # steps: 1-type, 2-client, 3-date (optionnelle), 4-lignes
        wiz = session.get('wiz', {'step': 1})
        step = int(request.args.get('step', wiz.get('step', 1)))

        # Préselection depuis ?client_id=...
        q_cid = request.args.get('client_id', type=int)
        if q_cid and not wiz.get('client_id'):
            wiz['client_id'] = q_cid

        if request.method == 'POST':
            action = request.form.get('action', 'next')
            if action == 'prev':
                step = max(1, step - 1)
            else:
                if step == 1:
                    mtype = request.form.get('type')
                    if mtype not in ('OUT', 'IN', 'DEFECT', 'FULL'):
                        flash("Choisir Livraison, Reprise, Défectueux ou Retour plein.", "warning")
                    else:
                        wiz['type'] = mtype
                        step = 2
                elif step == 2:
                    client_id = request.form.get('client_id', type=int)
                    if not client_id:
                        flash("Choisir un client.", "warning")
                    else:
                        wiz['client_id'] = client_id
                        step = 3
                elif step == 3:
                    d = request.form.get('date', '').strip()
                    # Date optionnelle : si vide, on utilisera 'now'
                    wiz['date'] = d or ''
                    step = 4
                elif step == 4:
                    variant_ids = request.form.getlist('variant_id')
                    qtys = request.form.getlist('qty')
                    unit_prices = request.form.getlist('unit_price_ttc')
                    deposits = request.form.getlist('deposit_per_keg')
                    notes = request.form.get('notes') or None

                    # Date finale
                    if wiz.get('date'):
                        try:
                            y, m_, d2 = [int(x) for x in wiz['date'].split('-')]
                            created_at = datetime.combine(date(y, m_, d2), time(hour=12))
                        except Exception:
                            created_at = datetime.utcnow()
                    else:
                        created_at = datetime.utcnow()

                    client_id = int(wiz['client_id'])
                    mtype = wiz['type']

                    created = 0
                    for i, vid in enumerate(variant_ids):
                        try:
                            vid_int = int(vid)
                            qty_int = int(qtys[i]) if i < len(qtys) else 0
                            if qty_int <= 0:
                                continue
                            # Valeurs par défaut côté serveur :
                            v = Variant.query.get(vid_int)
                            up = float(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else None
                            dep = float(deposits[i]) if i < len(deposits) and deposits[i] else None

                            # Lors d'une LIVRAISON (OUT), imposer d'office :
                            # - prix unitaire = prix variante si non saisi
                            # - consigne = 30 € si non saisie
                            if mtype == 'OUT':
                                if up is None:
                                    up = v.price_ttc if v and v.price_ttc is not None else None
                                if dep is None:
                                    dep = 30.0
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
                        created += 1

                        # Impact stock bar :
                        # - OUT : baisse
                        # - FULL (retour plein) : augmente
                        # - IN/DEFECT : pas d'impact (reprise vide ou défectueuse à gérer hors stock bar)
                        if mtype == 'OUT':
                            inv = get_or_create_inventory(vid_int)
                            inv.qty = (inv.qty or 0) - qty_int
                        elif mtype == 'FULL':
                            inv = get_or_create_inventory(vid_int)
                            inv.qty = (inv.qty or 0) + qty_int

                    db.session.commit()
                    session.pop('wiz', None)
                    flash(f"{created} ligne(s) enregistrée(s).", "success")
                    return redirect(url_for('client_detail', client_id=client_id))

            wiz['step'] = step
            session['wiz'] = wiz

        if step == 1:
            return render_template('movement_wizard.html', step=1, wiz=wiz)
        elif step == 2:
            clients = Client.query.order_by(Client.name).all()
            return render_template('movement_wizard.html', step=2, wiz=wiz, clients=clients)
        elif step == 3:
            return render_template('movement_wizard.html', step=3, wiz=wiz)
        else:
            variants = (
                db.session.query(Variant)
                .join(Product, Variant.product_id == Product.id)
                .order_by(Product.name, Variant.size_l)
                .all()
            )
            return render_template('movement_wizard.html', step=4, wiz=wiz, variants=variants)

    @app.route('/movement/<int:movement_id>/confirm-delete')
    def movement_confirm_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        return render_template('movement_confirm_delete.html', m=m)

    @app.route('/movement/<int:movement_id>/delete', methods=['POST'])
    def movement_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        cid = m.client_id
        # Reverser l'impact stock si c'était OUT / FULL
        if m.type == 'OUT':
            inv = get_or_create_inventory(m.variant_id)
            inv.qty = (inv.qty or 0) + (m.qty or 0)
        elif m.type == 'FULL':
            inv = get_or_create_inventory(m.variant_id)
            inv.qty = (inv.qty or 0) - (m.qty or 0)
        db.session.delete(m)
        db.session.commit()
        flash("Mouvement supprimé.", "success")
        return redirect(url_for('client_detail', client_id=cid))

    # ---- Stock bar (inventaire) ----
    @app.route('/stock', methods=['GET', 'POST'])
    def stock():
        if request.method == 'POST':
            # Mise à jour inventaire
            for v in Variant.query.all():
                val = request.form.get(f'qty_{v.id}')
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
            return redirect(url_for('stock'))

        variants = (
            db.session.query(Variant)
            .join(Product, Variant.product_id == Product.id)
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        items = []
        rules = {r.variant_id: r for r in ReorderRule.query.all()}
        for v in variants:
            inv = get_or_create_inventory(v.id)
            rule = rules.get(v.id)
            items.append(dict(variant=v, inventory=inv, rule=rule))
        alerts = compute_reorder_alerts()
        return render_template('stock.html', items=items, alerts=alerts)

    # ---- Catalogue (gardé pour compat, lien retiré dans navbar) ----
    @app.route('/catalog')
    def catalog():
        rows = (
            db.session.query(Product.name, Variant.size_l, Variant.price_ttc)
            .join(Variant, Variant.product_id == Product.id)
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        return render_template('catalog.html', rows=rows)

    @app.route('/products')
    def products():
        rows = (
            db.session.query(Product.name, Variant.size_l, Variant.price_ttc)
            .join(Variant, Variant.product_id == Product.id)
            .order_by(Product.name, Variant.size_l)
            .all()
        )
        return render_template('product.html', rows=rows)

    @app.errorhandler(404)
    def _404(_e):
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def _500(_e):
        return render_template('500.html'), 500

    return app


# ------------------ Seeding ------------------
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

    # Products + Variants
    if db.session.query(Product).count() == 0:
        for pname, sizes in DEFAULT_CATALOG:
            p = Product(name=pname)
            db.session.add(p)
            db.session.flush()
            for size in sizes:
                v = Variant(product_id=p.id, size_l=size, price_ttc=None)
                db.session.add(v)
        db.session.commit()

    # Inventory rows
    for v in Variant.query.all():
        if not Inventory.query.filter_by(variant_id=v.id).first():
            db.session.add(Inventory(variant_id=v.id, qty=0))
    db.session.commit()

    # Reorder rules
    for pname, size, minq in REORDER_DEFAULTS:
        v = (
            db.session.query(Variant)
            .join(Product, Variant.product_id == Product.id)
            .filter(Product.name == pname, Variant.size_l == size)
            .first()
        )
        if v:
            rule = ReorderRule.query.filter_by(variant_id=v.id).first()
            if not rule:
                db.session.add(ReorderRule(variant_id=v.id, min_qty=minq))
            else:
                rule.min_qty = minq
    db.session.commit()


# ------------------ Entrée ------------------
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
