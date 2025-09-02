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
    ("Coreff Ambrée", 22, 78), # 22L uniquement
    ("Cidre Val de Rance", 20, 96),
]

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
        # Valorisation: BIÈRE = OUT seulement ; CONSIGNE = OUT +, IN -
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

        # Fûts en cours chez eux (toutes variantes confondues)
        in_place_total = db.session.query(
            func.coalesce(func.sum(case(
                (Movement.type=='OUT', Movement.qty),
                else_=-Movement.qty
            )), 0)
        ).filter(Movement.client_id==client_id).scalar()

        # dernières dates
        last_delivery_at = db.session.query(func.max(Movement.created_at))\
            .filter(Movement.client_id==client_id, Movement.type=='OUT').scalar()
        last_pickup_at = db.session.query(func.max(Movement.created_at))\
            .filter(Movement.client_id==client_id, Movement.type=='IN').scalar()

        return render_template('client_detail.html',
                               client=client, rows=rows, movements=movements,
                               beer_billed_cum=beer_billed_cum,
                               deposit_in_play=deposit_in_play,
                               delivered_qty_cum=delivered_qty_cum,
                               in_place_total=in_place_total,
                               last_delivery_at=last_delivery_at, last_pickup_at=last_pickup_at)

    @app.route('/movement/new', methods=['GET','POST'])
    def movement_new():
        # client pré-sélectionné (depuis la fiche client)
        current_client = None
        client_id_param = request.args.get('client_id')
        if client_id_param:
            current_client = Client.query.get(int(client_id_param))

        if request.method == 'POST':
            # client choisi: soit depuis hidden input si on vient d'une fiche, soit via select
            posted_client_id = request.form.get('client_id')
            if posted_client_id:
                client_id = int(posted_client_id)
            elif current_client:
                client_id = current_client.id
            else:
                flash("Client manquant.", "danger")
                return redirect(url_for('movement_new'))

            # 1) Déterminer la variante (catalogue OU hors catalogue)
            custom_name = (request.form.get('custom_name') or '').strip()
            if custom_name:
                # Saisie hors catalogue
                custom_size = int(request.form.get('custom_size_l', '0') or 0)
                if custom_size <= 0:
                    flash("Format (L) invalide pour la référence hors catalogue.", "danger")
                    return redirect(url_for('movement_new', client_id=client_id))
                unit_price_raw = (request.form.get('unit_price_ttc') or '').strip()
                unit_price = float(unit_price_raw) if unit_price_raw else 0.0
                deposit_per_keg = float(request.form.get('deposit_per_keg', 30) or 30)

                # Créer / retrouver le Product (nom unique)
                product = Product.query.filter(func.lower(Product.name)==custom_name.lower()).first()
                if not product:
                    product = Product(name=custom_name)
                    db.session.add(product)
                    db.session.flush()
                # Créer / retrouver le Variant (même taille)
                variant = Variant.query.filter_by(product_id=product.id, size_l=custom_size).first()
                if not variant:
                    variant = Variant(product_id=product.id, size_l=custom_size, price_ttc=unit_price)
                    db.session.add(variant)
                    db.session.flush()
                else:
                    # Mettre à jour le prix catalogue si on a saisi un prix
                    if unit_price and (variant.price_ttc != unit_price):
                        variant.price_ttc = unit_price
                        db.session.flush()
                variant_id = variant.id
            else:
                # Catalogue existant
                variant_id = int(request.form['variant_id'])
                deposit_per_keg = float(request.form.get('deposit_per_keg', 30) or 30)
                # si prix vide on prendra celui du catalogue
                unit_price_raw = (request.form.get('unit_price_ttc') or '').strip()
                variant = Variant.query.get_or_404(variant_id)
                unit_price = float(unit_price_raw) if unit_price_raw else (variant.price_ttc if variant.price_ttc is not None else 0.0)

            # 2) Quantité + Type + Date
            qty = int(request.form.get('qty', 1))
            mtype = request.form['type']  # 'OUT' ou 'IN'

            # 2b) Date/heure choisie (datetime-local: 'YYYY-MM-DDTHH:MM')
            created_at = datetime.utcnow()
            date_str = (request.form.get('when') or '').strip()
            if date_str:
                try:
                    created_at = datetime.strptime(date_str, "%Y-%m-%dT%H:%M")
                except ValueError:
                    flash("Format de date invalide.", "danger")
                    return redirect(url_for('movement_new', client_id=client_id))

            # 3) Validation "reprise <= stock variant du client"
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

            # 4) Enregistrer le mouvement
            m = Movement(
                created_at=created_at,
                type=mtype,
                client_id=client_id,
                variant_id=variant_id,
                qty=qty,
                unit_price_ttc=unit_price,       # OUT: valorise bière ; IN : ignoré dans les totaux bière
                deposit_per_keg=deposit_per_keg,
                notes=(request.form.get('notes','').strip() or None)
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
