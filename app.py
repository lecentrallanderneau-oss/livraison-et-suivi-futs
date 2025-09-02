import os
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import func, case
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
    # (product_name, size_l, price_ttc)
    ("Coreff Blonde", 20, 68),
    ("Coreff Blonde", 30, 102),
    ("Coreff Blonde Bio", 20, 74),
    ("Coreff Blonde Bio", 30, 110),
    ("Coreff IPA", 20, 85),
    ("Coreff IPA", 30, 127),
    ("Coreff Blanche", 20, 81),
    ("Coreff Rousse", 20, 82),          # 20L uniquement
    ("Coreff Ambrée", 22, None),        # 22L uniquement (prix à renseigner si dispo)
    ("Cidre Val de Rance", 20, 96),
]

def seed_if_empty():
    if Client.query.count() == 0 and Product.query.count() == 0 and Variant.query.count() == 0:
        # Clients
        for c in DEFAULT_CLIENTS:
            db.session.add(Client(name=c))
        db.session.flush()
        # Produits
        prods = {}
        for n in DEFAULT_PRODUCTS:
            p = Product(name=n)
            db.session.add(p)
            prods[n] = p
        db.session.flush()
        # Variants
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
    def not_found(e): 
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def server_err(e): 
        return render_template('500.html'), 500

    @app.route('/')
    def index():
        rows = db.session.query(
            Client.id, Client.name,
            func.coalesce(func.sum(case((Movement.type=='OUT', Movement.qty), else_=0)),0).label('out_qty'),
            func.coalesce(func.sum(case((Movement.type=='IN', Movement.qty), else_=0)),0).label('in_qty'),
            func.coalesce(func.sum(
                case((Movement.type=='OUT', Movement.qty*Movement.deposit_per_keg),
                     else_=-Movement.qty*Movement.deposit_per_keg)
            ),0.0).label('deposit_in_play')
        ).join(Movement, Movement.client_id==Client.id, isouter=True)\
         .group_by(Client.id, Client.name).order_by(Client.name).all()
        return render_template('index.html', rows=rows)

    @app.route('/clients')
    def clients():
        return render_template('clients.html', clients=Client.query.order_by(Client.name).all())

    @app.route('/client/<int:client_id>')
    def client_detail(client_id):
        client = Client.query.get_or_404(client_id)
        q = db.session.query(
            Variant.id, Product.name.label('product_name'), Variant.size_l,
            func.coalesce(func.sum(case((Movement.type=='OUT', Movement.qty), else_=0)),0).label('out_qty'),
            func.coalesce(func.sum(case((Movement.type=='IN', Movement.qty), else_=0)),0).label('in_qty'),
            func.min(Variant.price_ttc).label('catalog_price')
        ).join(Product, Product.id==Variant.product_id)\
         .join(Movement, Movement.variant_id==Variant.id, isouter=True)\
         .filter((Movement.client_id==client_id) | (Movement.client_id==None))\
         .group_by(Variant.id, Product.name, Variant.size_l).order_by(Product.name, Variant.size_l)
        rows = q.all()
        return render_template('client_detail.html', client=client, rows=rows)

    @app.route('/movement/new', methods=['GET','POST'])
    def movement_new():
        if request.method == 'POST':
            variant_id = int(request.form['variant_id'])
            v = Variant.query.get_or_404(variant_id)
            unit_price_raw = request.form.get('unit_price_ttc', '').strip()
            unit_price = float(unit_price_raw) if unit_price_raw else (v.price_ttc if v.price_ttc is not None else None)

            m = Movement(
                type=request.form['type'],
                client_id=int(request.form['client_id']),
                variant_id=variant_id,
                qty=int(request.form.get('qty', 1)),
                unit_price_ttc=unit_price,
                deposit_per_keg=float(request.form.get('deposit_per_keg', 30) or 30),
                notes=(request.form.get('notes','').strip() or None)
            )
            db.session.add(m)
            db.session.commit()
            flash('Mouvement enregistré ✅')
            return redirect(url_for('client_detail', client_id=m.client_id))

        clients = Client.query.order_by(Client.name).all()
        variants = db.session.query(Variant.id, Product.name, Variant.size_l, Variant.price_ttc)\
                             .join(Product).order_by(Product.name, Variant.size_l).all()
        return render_template('movement_new.html', clients=clients, variants=variants)

    @app.route('/products')
    def products():
        rows = db.session.query(Product.name, Variant.size_l, Variant.price_ttc)\
                         .join(Variant).order_by(Product.name, Variant.size_l).all()
        return render_template('products.html', rows=rows)

    return app

app = create_app()
