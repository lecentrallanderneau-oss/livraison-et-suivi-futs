import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import func, case, and_, update, or_
from models import db, Client, Product, Variant, Movement

# --------- Données par défaut ----------
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
    ("Coreff Rousse", 20, 82),     # 20L uniquement
    ("Coreff Ambrée", 22, 78),     # 22L uniquement -> PRIX = 78 €
    ("Cidre Val de Rance", 20, 96),
]

# Matériel encodé dans Movement.notes : ||EQ|tireuse=1;co2=2;comptoir=0;tonnelle=1
EQ_KEYS = ("tireuse", "co2", "comptoir", "tonnelle")

# --------- Initialisation catalogue ----------
def seed_if_empty():
    if Client.query.count() == 0:
        for name in DEFAULT_CLIENTS:
            db.session.add(Client(name=name))
        db.session.commit()

    if Product.query.count() == 0:
        prods = {}
        for name in DEFAULT_PRODUCTS:
            p = Product(name=name)
            db.session.add(p)
            db.session.flush()
            prods[name] = p
        db.session.flush()
        for name, size, price in DEFAULT_VARIANTS:
            db.session.add(Variant(product_id=prods[name].id, size_l=size, price_ttc=price))
        db.session.commit()


def get_or_create_equipment_placeholder():
    """Variant spécial pour mouvements 'matériel seul' (0L, 0€)."""
    prod = Product.query.filter(func.lower(Product.name) == "matériel seul").first()
    if not prod:
        prod = Product(name="Matériel seul")
        db.session.add(prod)
        db.session.flush()
    v = Variant.query.filter_by(product_id=prod.id, size_l=0).first()
    if not v:
        v = Variant(product_id=prod.id, size_l=0, price_ttc=0.0)
        db.session.add(v)
        db.session.flush()
        db.session.commit()
    return v


def _get_or_create_product_variant(name: str, price: float):
    """Assure l'existence d'un produit 0L au prix donné."""
    prod = Product.query.filter(func.lower(Product.name) == name.lower()).first()
    if not prod:
        prod = Product(name=name)
        db.session.add(prod); db.session.flush()
    var = Variant.query.filter_by(product_id=prod.id, size_l=0).first()
    if not var:
        var = Variant(product_id=prod.id, size_l=0, price_ttc=price)
        db.session.add(var); db.session.flush()
    else:
        if var.price_ttc != price:
            var.price_ttc = price; db.session.flush()
    return var


def ensure_catalog_fixes():
    """Corrections ponctuelles de catalogue (prix, variantes manquantes, etc.)."""
    # Exemple : s’assurer que Coreff Ambrée 22L = 78 €
    prod = Product.query.filter(func.lower(Product.name) == "coreff ambrée").first()
    if prod:
        v = Variant.query.filter_by(product_id=prod.id, size_l=22).first()
        if v and (v.price_ttc is None or float(v.price_ttc) != 78.0):
            v.price_ttc = 78.0
            db.session.add(v)
    db.session.commit()


# --------- Agrégations / helpers ----------
def liters_for(mtype, size_l, qty):
    if mtype == 'OUT':
        return size_l * qty
    if mtype == 'IN':
        return -size_l * qty
    if mtype == 'DEFECT':
        return -size_l * qty
    return 0

def compute_cum_beer_billed(client_id: int):
    """Total bière facturée cumulée (tous OUT de variants > 0L) - (IN/DEFECT)."""
    out_sum = db.session.query(
        func.coalesce(func.sum(case(
            (and_(Movement.type == 'OUT', Variant.size_l > 0), Movement.qty * Variant.price_ttc),
            else_=0
        )), 0)
    ).join(Variant, Variant.id == Movement.variant_id) \
     .filter(Movement.client_id == client_id).scalar()

    in_def_sum = db.session.query(
        func.coalesce(func.sum(case(
            (and_(or_(Movement.type == 'IN', Movement.type == 'DEFECT'), Variant.size_l > 0), Movement.qty * Variant.price_ttc),
            else_=0
        )), 0)
    ).join(Variant, Variant.id == Movement.variant_id) \
     .filter(Movement.client_id == client_id).scalar()

    out_sum = float(out_sum or 0.0)
    in_def_sum = float(in_def_sum or 0.0)
    return max(0.0, out_sum - in_def_sum)

def sum_equipment_for_client(client_id: int):
    """Matériel prêté par client (toutes clés EQ_KEYS)."""
    totals = {k: 0 for k in EQ_KEYS}
    movs = Movement.query.filter(Movement.client_id == client_id).order_by(Movement.created_at).all()
    for m in movs:
        eq, _ = unpack_equipment(m.notes or "")
        sign = 1 if m.type == 'OUT' else -1
        for k in EQ_KEYS:
            totals[k] += sign * int(eq.get(k, 0) or 0)
    for k in list(totals.keys()):
        if totals[k] < 0:
            totals[k] = 0
    return totals

def pack_equipment(human_notes, eq_dict):
    """Concatène la partie humaine + bloc EQ."""
    clean = (human_notes or "").strip()
    parts = []
    for k in EQ_KEYS:
        v = int(eq_dict.get(k, 0) or 0)
        if v != 0:
            parts.append(f"{k}={v}")
    if parts:
        eq_block = "||EQ|" + ";".join(parts)
        return (clean + " " + eq_block).strip()
    return clean


def unpack_equipment(notes_text):
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


def ensure_data_consistency():
    """Petits correctifs de données (prix NULL → prix catalogue, etc.)."""
    # Mettre à jour les Movement.unit_price_ttc manquants pour variants > 0L
    sub = db.session.query(Variant.id.label("vid"), Variant.price_ttc.label("catalog_price")).subquery()
    db.session.execute(
        update(Movement)
        .where(Movement.unit_price_ttc.is_(None))
        .where(Movement.variant_id == sub.c.vid)
        .values(unit_price_ttc=sub.c.catalog_price)
    )
    db.session.commit()


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "devkey")
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()
        seed_if_empty()
        ensure_catalog_fixes()
        ensure_data_consistency()

    @app.errorhandler(404)
    def not_found(e): 
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def server_err(e): 
        return render_template('500.html'), 500

    # --------- Routes ----------

    @app.route('/')
    def index():
        # Récap par client (badges + dates + matériel), sans gobelets
        clients = Client.query.order_by(Client.name).all()
        cards = []

        for c in clients:
            # total fûts livrés cumulés (OUT uniquement, variants > 0L)
            total_kegs_delivered = db.session.query(
                func.coalesce(func.sum(case(
                    (and_(Movement.type == 'OUT', Variant.size_l > 0), Movement.qty),
                    else_=0
                )), 0)
            ).join(Variant, Variant.id == Movement.variant_id) \
             .filter(Movement.client_id == c.id).scalar() or 0

            # bière facturée cumulée (en €)
            beer_eur = compute_cum_beer_billed(c.id)

            # consignes en cours (somme OUT de qty * deposit_per_keg)
            deposit_in_play = db.session.query(
                func.coalesce(func.sum(case(
                    (Movement.type == 'OUT', Movement.qty * (Movement.deposit_per_keg or 0.0)),
                    else_=0
                )), 0.0)
            ).filter(Movement.client_id == c.id).scalar() or 0.0

            # dates
            last_delivery_at = db.session.query(func.max(Movement.created_at)) \
                .filter(Movement.client_id == c.id, Movement.type == 'OUT').scalar()
            last_pickup_at = db.session.query(func.max(Movement.created_at)) \
                .filter(Movement.client_id == c.id, or_(Movement.type == 'IN', Movement.type == 'DEFECT')).scalar()

            # matériel total
            eq_totals = sum_equipment_for_client(c.id)

            cards.append({
                "id": c.id,
                "name": c.name,
                "kegs": int(total_kegs_delivered),
                "beer_eur": float(beer_eur or 0),
                "deposit_eur": float(deposit_in_play or 0),
                "last_out": last_delivery_at,
                "last_in": last_pickup_at,
                "eq": eq_totals
            })

        return render_template('index.html', cards=cards)

    @app.route('/clients')
    def clients():
        clients = Client.query.order_by(Client.name).all()
        return render_template('clients.html', clients=clients)

    @app.route('/catalog')
    def catalog():
        rows = db.session.query(Product.name, Variant.size_l, Variant.price_ttc) \
            .join(Variant).order_by(Product.name, Variant.size_l).all()
        return render_template('catalog.html', rows=rows)

    @app.route('/client/<int:client_id>')
    def client_detail(client_id):
        client = Client.query.get_or_404(client_id)

        # STOCK PAR PRODUIT — on EXCLUT variants 0L
        q = db.session.query(
            Variant.id, Product.name.label('product_name'), Variant.size_l,
            func.coalesce(func.sum(case((and_(Movement.type == 'OUT', Variant.size_l > 0), Movement.qty), else_=0)), 0).label('out_qty'),
            func.coalesce(func.sum(case((and_(or_(Movement.type == 'IN', Movement.type == 'DEFECT'), Variant.size_l > 0), Movement.qty), else_=0)), 0).label('in_qty'),
            func.min(Variant.price_ttc).label('catalog_price')
        ).join(Product, Product.id == Variant.product_id) \
         .join(Movement, Movement.variant_id == Variant.id, isouter=True) \
         .filter(Variant.size_l > 0, Movement.client_id == client_id) \
         .group_by(Variant.id, Product.name, Variant.size_l) \
         .order_by(Product.name, Variant.size_l)
        rows = q.all()

        # Historique des mouvements (récents d’abord)
        movs = Movement.query.filter(Movement.client_id == client_id) \
                             .order_by(Movement.created_at.desc()).all()

        display_movs = []
        delivered_qty_cum = 0
        in_place_total = 0
        for m in movs:
            v = Variant.query.get(m.variant_id)
            p = Product.query.get(v.product_id) if v else None
            liters = (v.size_l * m.qty) if (v and v.size_l) else 0
            if m.type == 'OUT':
                delivered_qty_cum += liters
                in_place_total += liters
            elif m.type in ('IN', 'DEFECT'):
                in_place_total -= liters
            eq, human_note = unpack_equipment(m.notes or "")
            display_movs.append({
                "id": m.id,
                "when": m.created_at,
                "type": m.type,
                "product": p.name if p else "N/A",
                "size_l": v.size_l if v else 0,
                "qty": m.qty,
                "unit_price": m.unit_price_ttc,
                "deposit_per_keg": m.deposit_per_keg or 0.0,
                "liters": liters,
                "equipment": eq,
                "note": human_note,
            })

        beer_billed_cum = compute_cum_beer_billed(client_id)
        deposit_in_play = db.session.query(
            func.coalesce(func.sum(case(
                (Movement.type == 'OUT', Movement.qty * (Movement.deposit_per_keg or 0.0)),
                else_=0
            )), 0.0)
        ).filter(Movement.client_id == client_id).scalar() or 0.0

        equipment_totals = sum_equipment_for_client(client_id)
        last_delivery_at = db.session.query(func.max(Movement.created_at)) \
            .filter(Movement.client_id == client_id, Movement.type == 'OUT').scalar()
        last_pickup_at = db.session.query(func.max(Movement.created_at)) \
            .filter(Movement.client_id == client_id, or_(Movement.type == 'IN', Movement.type == 'DEFECT')).scalar()

        return render_template('client_detail.html',
                               client=client, rows=rows, movements=display_movs,
                               beer_billed_cum=beer_billed_cum,
                               deposit_in_play=deposit_in_play,
                               delivered_qty_cum=delivered_qty_cum,
                               in_place_total=in_place_total,
                               equipment_totals=equipment_totals,
                               last_delivery_at=last_delivery_at, last_pickup_at=last_pickup_at)

    @app.route('/movement/new', methods=['GET', 'POST'])
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
                return redirect(url_for('clients'))

            variant_id = int(request.form.get('variant_id'))
            qty = int(request.form.get('qty'))
            unit_price = request.form.get('unit_price_ttc')
            unit_price = float(unit_price) if unit_price not in (None, "",) else None
            deposit_per_keg = float(request.form.get('deposit_per_keg') or 0.0)
            mtype = request.form.get('type')  # OUT, IN, DEFECT
            created_at = datetime.strptime(request.form.get('created_at'), "%Y-%m-%dT%H:%M")
            notes = (request.form.get('notes') or "").strip()

            # Matériel / notes
            eq = {k: int(request.form.get(f"eq_{k}") or 0) for k in EQ_KEYS}
            human_notes = notes
            if any(v != 0 for v in eq.values()):
                notes = pack_equipment(human_notes, eq)

            # --- Validations basiques ---
            if qty <= 0:
                flash("Quantité invalide.", "danger")
                return redirect(url_for('movement_new', client_id=client_id))
            if mtype not in ('OUT', 'IN', 'DEFECT'):
                flash("Type de mouvement invalide.", "danger")
                return redirect(url_for('movement_new', client_id=client_id))

            # Mouvement principal
            main_m = Movement(
                created_at=created_at,
                type=mtype,
                client_id=client_id,
                variant_id=variant_id,
                qty=qty,
                unit_price_ttc=unit_price,
                deposit_per_keg=deposit_per_keg,
                notes=notes
            )
            db.session.add(main_m)
            db.session.flush()  # id dispo

            # (plus aucune logique gobelets)

            db.session.commit()
            flash('Mouvement enregistré ✅', "success")
            return redirect(url_for('client_detail', client_id=client_id))

        # GET
        clients = Client.query.order_by(Client.name).all()
        variants = db.session.query(Variant.id, Product.name, Variant.size_l, Variant.price_ttc) \
                             .join(Product).order_by(Product.name, Variant.size_l).all()
        return render_template('movement_new.html', clients=clients, variants=variants, current_client=current_client)

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
        rows = db.session.query(Product.name, Variant.size_l, Variant.price_ttc) \
            .join(Variant).order_by(Product.name, Variant.size_l).all()
        return render_template('products.html', rows=rows)

    return app


# --------- Entrée ---------
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
