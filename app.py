import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import func, case, and_
from models import db, Client, Product, Variant, Movement

# --- Données par défaut ---
DEFAULT_CLIENTS = [
    "Landerneau Football Club",
    "Maison Michel",
    "Ploudiry / Sizun Handball",
]

DEFAULT_PRODUCTS = [
    "Coreff Blonde",
    "Coreff Blonde Bio",
    "Coreff IPA",
    "Coreff Blanche",
    "Coreff Rousse",
    "Coreff Ambrée",
    "Cidre Val de Rance",
]

DEFAULT_VARIANTS = [
    ("Coreff Blonde", 20, 68),
    ("Coreff Blonde", 30, 102),
    ("Coreff Blonde Bio", 20, 74),
    ("Coreff Blonde Bio", 30, 110),
    ("Coreff IPA", 20, 85),
    ("Coreff IPA", 30, 127),
    ("Coreff Blanche", 20, 81),
    ("Coreff Rousse", 20, 82),   # 20L uniquement
    ("Coreff Ambrée", 22, None), # 22L uniquement
    ("Cidre Val de Rance", 20, 96),
]

EQ_KEYS = ("tireuse", "co2", "comptoir", "tonnelle")

def seed_if_empty():
    if Client.query.count() == 0 and Product.query.count() == 0 and Variant.query.count() == 0:
        for c in DEFAULT_CLIENTS:
            db.session.add(Client(name=c))
        db.session.flush()
        prods = {}
        for n in DEFAULT_PRODUCTS:
            p = Product(name=n); db.session.add(p); prods[n] = p
        db.session.flush()
        for name, size, price in DEFAULT_VARIANTS:
            db.session.add(Variant(product_id=prods[name].id, size_l=size, price_ttc=price))
        db.session.commit()

# ---- Encodage/decodage matériel dans Movement.notes ----
def pack_equipment(notes_text: str, eq: dict) -> str:
    """Fusionne la note lisible et le bloc matériel '||EQ|k=v;...'. Supprime les zéros pour alléger."""
    clean = (notes_text or "").strip()
    parts = []
    for k in EQ_KEYS:
        v = int(eq.get(k, 0) or 0)
        if v != 0:
            parts.append(f"{k}={v}")
    if parts:
        eq_block = "||EQ|" + ";".join(parts)
        return (clean + " " + eq_block).strip()
    return clean

def unpack_equipment(notes_text: str) -> tuple[dict, str]:
    """Retourne ({k:int}, note_sans_bloc). Si pas de bloc, eq=0 et note intacte."""
    eq = {k: 0 for k in EQ_KEYS}
    if not notes_text:
        return eq, ""
    txt = notes_text
    sep = "||EQ|"
    if sep in txt:
        human, eqpart = txt.split(sep, 1)
        human = human.strip()
        for pair in eqpart.strip().split(";"):
            if "=" in pair:
                k, val = pair.split("=", 1)
                k = k.strip().lower()
                try:
                    val = int(val.strip())
                except:
                    val = 0
                if k in eq:
                    eq[k] = val
        return eq, human
    return eq, txt.strip()

def sum_equipment_for_client(client_id: int) -> dict:
    """Somme nette du matériel chez le client (OUT +, IN -)."""
    totals = {k: 0 for k in EQ_KEYS}
    movements = Movement.query.filter_by(client_id=client_id).all()
    for m in movements:
        eq, _ = unpack_equipment(m.notes)
        sign = 1 if m.type == 'OUT' else -1
        for k in EQ_KEYS:
            totals[k] += sign * int(eq.get(k, 0) or 0)
    # Pas de négatif dans l'affichage
    for k in EQ_KEYS:
        if totals[k] < 0:
            totals[k] = 0
    return totals

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'devkey')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///data.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()
        seed_if_empty()

    @app.errorhandler(404)
    def not_found(e): return render_template('404.html'), 404

    @app.errorhandler(500)
    def server_err(e): return render_template('500.html'), 500

    @app.route('/')
    def index():
        # BIÈRE: OUT seulement ; CONSIGNE: OUT +, IN -
        beer_value_expr = case(
            (Movement.type == 'OUT', Movement.qty * func.coalesce(Movement.unit_price_ttc, 0.0)),
            else_=0.0
        )
        deposit_value_expr = case(
            (Movement.type == 'OUT', Movement.qty * Movement.deposit_per_keg),
            else_=-Movement.qty * Movement.deposit_per_keg
        )
        last_delivery = func.max(case((Movement.type == 'OUT', Movement.created_at), else_=None))
        last_pickup   = func.max(case((Movement.type == 'IN',  Movement.created_at), else_=None))

        total_delivered_qty = func.coalesce(func.sum(case((Movement.type=='OUT', Movement.qty), else_=0)), 0)
        total_beer_billed   = func.coalesce(func.sum(case(
            (Movement.type=='OUT', Movement.qty * func.coalesce(Movement.unit_price_ttc, 0.0)), else_=0.0
        )), 0.0)

        rows = db.session.query(
            Client.id, Client.name,
            func.coalesce(func.sum(case((Movement.type=='OUT', Movement.qty), else_=0)),0).label('out_qty'),
            func.coalesce(func.sum(case((Movement.type=='IN',  Movement.qty), else_=0)),0).label('in_qty'),
            func.coalesce(func.sum(deposit_value_expr), 0.0).label('deposit_in_play'),
            func.coalesce(func.sum(beer_value_expr), 0.0).label('beer_billed_cum'),
            last_delivery.label('last_delivery_at'),
            last_pickup.label('last_pickup_at'),
            total_delivered_qty.label('delivered_qty_cum'),
            total_beer_billed.label('beer_billed_total')
        ).join(Movement, Movement.client_id==Client.id, isouter=True)\
         .group_by(Client.id, Client.name).order_by(Client.name).all()

        return render_template('index.html', rows=rows)

    @app.route('/clients', methods=['GET','POST'])
    def clients():
        if request.method == 'POST':
            name = (request.form.get('name') or '').strip()
            if not name:
                flash("Nom de client requis.", "danger")
                return redirect(url_for('clients'))
            exists = Client.query.filter(func.lower(Client.name)==name.lower()).first()
            if exists:
                flash("Ce client existe déjà.", "warning")
                return redirect(url_for('clients'))
            db.session.add(Client(name=name))
            db.session.commit()
            flash("Client ajouté ✅", "success")
            return redirect(url_for('clients'))

        clis = Client.query.order_by(Client.name).all()
        return render_template('clients.html', clients=clis)

    @app.route('/client/<int:client_id>')
    def client_detail(client_id):
        client = Client.query.get_or_404(client_id)

        # Stock par variant
        q = db.session.query(
            Variant.id, Product.name.label('product_name'), Variant.size_l,
            func.coalesce(func.sum(case((Movement.type=='OUT', Movement.qty), else_=0)),0).label('out_qty'),
            func.coalesce(func.sum(case((Movement.type=='IN',  Movement.qty), else_=0)),0).label('in_qty'),
            func.min(Variant.price_ttc).label('catalog_price')
        ).join(Product, Product.id==Variant.product_id)\
         .join(Movement, Movement.variant_id==Variant.id, isouter=True)\
         .filter((Movement.client_id==client_id) | (Movement.client_id==None))\
         .group_by(Variant.id, Product.name, Variant.size_l).order_by(Product.name, Variant.size_l)
        rows = q.all()

        # Historique complet
        movements = Movement.query.filter_by(client_id=client_id)\
                                  .order_by(Movement.created_at.desc()).all()
        # Enrichir les notes affichées (sans le bloc EQ)
        display_movs = []
        for m in movements:
            eq, human = unpack_equipment(m.notes)
            display_movs.append((m, eq, human))

        # Totaux
        beer_billed_cum = db.session.query(
            func.coalesce(func.sum(case(
                (Movement.type=='OUT', Movement.qty * func.coalesce(Movement.unit_price_ttc, 0.0)),
                else_=0.0
            )), 0.0)
        ).filter(Movement.client_id==client_id).scalar()

        deposit_in_play = db.session.query(
            func.coalesce(func.sum(case(
                (Movement.type=='OUT', Movement.qty * Movement.deposit_per_keg),
                else_=-Movement.qty * Movement.deposit_per_keg
            )), 0.0)
        ).filter(Movement.client_id==client_id).scalar()

        delivered_qty_cum = db.session.query(
            func.coalesce(func.sum(case((Movement.type=='OUT', Movement.qty), else_=0)), 0)
        ).filter(Movement.client_id==client_id).scalar()

        # Fûts en cours (toutes variantes)
        in_place_total = db.session.query(
            func.coalesce(func.sum(case(
                (Movement.type=='OUT', Movement.qty),
                else_=-Movement.qty
            )), 0)
        ).filter(Movement.client_id==client_id).scalar()

        # Matériel en cours (tireuse, co2, comptoir, tonnelle)
        equipment_totals = sum_equipment_for_client(client_id)

        # dernières dates
        last_delivery_at = db.session.query(func.max(Movement.created_at))\
            .filter(Movement.client_id==client_id, Movement.type=='OUT').scalar()
        last_pickup_at = db.session.query(func.max(Movement.created_at))\
            .filter(Movement.client_id==client_id, Movement.type=='IN').scalar()

        return render_template('client_detail.html',
                               client=client, rows=rows, movements=display_movs,
                               beer_billed_cum=beer_billed_cum,
                               deposit_in_play=deposit_in_play,
                               delivered_qty_cum=delivered_qty_cum,
                               in_place_total=in_place_total,
                               equipment_totals=equipment_totals,
                               last_delivery_at=last_delivery_at, last_pickup_at=last_pickup_at)

    @app.route('/movement/new', methods=['GET','POST'])
    def movement_new():
        current_client = None
        client_id_param = request.args.get('client_id')
        if client_id_param:
            current_client = Client.query.get(int(client_id_param))

        if request.method == 'POST':
            posted_client_id = request.form.get('client_id')
            if posted_client_id:
                client_id = int(posted_client_id)
            elif current_client:
                client_id = current_client.id
            else:
                flash("Client manquant.", "danger")
                return redirect(url_for('movement_new'))

            # 1) Variante (catalogue OU hors catalogue)
            custom_name = (request.form.get('custom_name') or '').strip()
            if custom_name:
                custom_size = int(request.form.get('custom_size_l', '0') or 0)
                if custom_size <= 0:
                    flash("Format (L) invalide pour la référence hors catalogue.", "danger")
                    return redirect(url_for('movement_new', client_id=client_id))
                unit_price_raw = (request.form.get('unit_price_ttc') or '').strip()
                unit_price = float(unit_price_raw) if unit_price_raw else 0.0
                deposit_per_keg = float(request.form.get('deposit_per_keg', 30) or 30)
                product = Product.query.filter(func.lower(Product.name)==custom_name.lower()).first()
                if not product:
                    product = Product(name=custom_name)
                    db.session.add(product); db.session.flush()
                variant = Variant.query.filter_by(product_id=product.id, size_l=custom_size).first()
                if not variant:
                    variant = Variant(product_id=product.id, size_l=custom_size, price_ttc=unit_price)
                    db.session.add(variant); db.session.flush()
                else:
                    if unit_price and (variant.price_ttc != unit_price):
                        variant.price_ttc = unit_price; db.session.flush()
                variant_id = variant.id
            else:
                variant_id = int(request.form['variant_id'])
                deposit_per_keg = float(request.form.get('deposit_per_keg', 30) or 30)
                unit_price_raw = (request.form.get('unit_price_ttc') or '').strip()
                variant = Variant.query.get_or_404(variant_id)
                unit_price = float(unit_price_raw) if unit_price_raw else (variant.price_ttc if variant.price_ttc is not None else 0.0)

            # 2) Quantité, Type, Date
            qty = int(request.form.get('qty', 1))
            mtype = request.form['type']  # 'OUT' ou 'IN'
            created_at = datetime.utcnow()
            date_str = (request.form.get('when') or '').strip()
            if date_str:
                try:
                    created_at = datetime.strptime(date_str, "%Y-%m-%dT%H:%M")
                except ValueError:
                    flash("Format de date invalide.", "danger")
                    return redirect(url_for('movement_new', client_id=client_id))

            # 3) Équipement saisi
            eq = {
                "tireuse": int(request.form.get('eq_tireuse', 0) or 0),
                "co2": int(request.form.get('eq_co2', 0) or 0),
                "comptoir": int(request.form.get('eq_comptoir', 0) or 0),
                "tonnelle": int(request.form.get('eq_tonnelle', 0) or 0),
            }

            # 4) Validation "reprise <= stock variant du client"
            if mtype == 'IN':
                in_place_variant = db.session.query(
                    func.coalesce(func.sum(case(
                        (Movement.type=='OUT', Movement.qty),
                        else_=-Movement.qty
                    )), 0)
                ).filter(and_(Movement.client_id==client_id, Movement.variant_id==variant_id)).scalar()
                if qty > max(in_place_variant, 0):
                    flash(f"Impossible de reprendre {qty} fût(s) — stock disponible pour cette référence: {max(in_place_variant, 0)}.", "danger")
                    return redirect(url_for('movement_new', client_id=client_id))
                # Validation matériel (ne pas aller en négatif)
                current_eq = sum_equipment_for_client(client_id)
                for k in EQ_KEYS:
                    want = int(eq.get(k, 0) or 0)
                    if want > current_eq.get(k, 0):
                        flash(f"Impossible de reprendre {want} {k}(s) — disponible chez le client: {current_eq.get(k,0)}.", "danger")
                        return redirect(url_for('movement_new', client_id=client_id))

            # 5) Enregistrer
            human_notes = (request.form.get('notes','').strip() or "")
            notes = pack_equipment(human_notes, eq)

            m = Movement(
                created_at=created_at,
                type=mtype,
                client_id=client_id,
                variant_id=variant_id,
                qty=qty,
                unit_price_ttc=unit_price,  # OUT valorise bière ; IN ignoré pour la bière
                deposit_per_keg=deposit_per_keg,
                notes=notes
            )
            db.session.add(m); db.session.commit()
            flash('Mouvement enregistré ✅', "success")
            return redirect(url_for('client_detail', client_id=client_id))

        clients = Client.query.order_by(Client.name).all()
        variants = db.session.query(Variant.id, Product.name, Variant.size_l, Variant.price_ttc)\
                             .join(Product).order_by(Product.name, Variant.size_l).all()
        return render_template('movement_new.html', clients=clients, variants=variants, current_client=current_client)

    # --- Suppression d'un mouvement (avec confirmation) ---
    @app.route('/movement/<int:movement_id>/confirm_delete')
    def movement_confirm_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        return render_template('movement_confirm_delete.html', m=m)

    @app.route('/movement/<int:movement_id>/delete', methods=['POST'])
    def movement_delete(movement_id):
        m = Movement.query.get_or_404(movement_id)
        client_id = m.client_id
        db.session.delete(m)
        db.session.commit()
        flash("Mouvement supprimé ✅", "success")
        return redirect(url_for('client_detail', client_id=client_id))

    @app.route('/products')
    def products():
        rows = db.session.query(Product.name, Variant.size_l, Variant.price_ttc)\
                         .join(Variant).order_by(Product.name, Variant.size_l).all()
        return render_template('products.html', rows=rows)

    return app

app = create_app()
