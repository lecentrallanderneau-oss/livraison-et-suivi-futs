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

# Matériel encodé dans Movement.notes : ||EQ|tireuse=1;co2=2;comptoir=0;tonnelle=1;ecocup=50
EQ_KEYS = ("tireuse", "co2", "comptoir", "tonnelle", "ecocup")

# Produits techniques Ecocup
ECOCUP_WASH_NAME = "Ecocup lavage"
ECOCUP_LOSS_NAME = "Ecocup perdu"
ECOCUP_WASH_PRICE = 0.10  # €/gobelet récupéré (lavage)
ECOCUP_LOSS_PRICE = 1.00  # €/gobelet manquant


# --------- Initialisation catalogue ----------
def seed_if_empty():
    if Client.query.count() == 0 and Product.query.count() == 0 and Variant.query.count() == 0:
        for c in DEFAULT_CLIENTS:
            db.session.add(Client(name=c))
        db.session.flush()
        prods = {}
        for n in DEFAULT_PRODUCTS:
            p = Product(name=n)
            db.session.add(p)
            prods[n] = p
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


def get_or_create_ecocup_variants():
    """Crée (si besoin) Ecocup lavage (0L, 0.10€) et Ecocup perdu (0L, 1.00€)."""
    # Lavage
    prod_w = Product.query.filter(func.lower(Product.name) == ECOCUP_WASH_NAME.lower()).first()
    if not prod_w:
        prod_w = Product(name=ECOCUP_WASH_NAME)
        db.session.add(prod_w); db.session.flush()
    var_w = Variant.query.filter_by(product_id=prod_w.id, size_l=0).first()
    if not var_w:
        var_w = Variant(product_id=prod_w.id, size_l=0, price_ttc=ECOCUP_WASH_PRICE)
        db.session.add(var_w); db.session.flush()
    else:
        if var_w.price_ttc != ECOCUP_WASH_PRICE:
            var_w.price_ttc = ECOCUP_WASH_PRICE; db.session.flush()

    # Perte
    prod_l = Product.query.filter(func.lower(Product.name) == ECOCUP_LOSS_NAME.lower()).first()
    if not prod_l:
        prod_l = Product(name=ECOCUP_LOSS_NAME)
        db.session.add(prod_l); db.session.flush()
    var_l = Variant.query.filter_by(product_id=prod_l.id, size_l=0).first()
    if not var_l:
        var_l = Variant(product_id=prod_l.id, size_l=0, price_ttc=ECOCUP_LOSS_PRICE)
        db.session.add(var_l); db.session.flush()
    else:
        if var_l.price_ttc != ECOCUP_LOSS_PRICE:
            var_l.price_ttc = ECOCUP_LOSS_PRICE; db.session.flush()

    db.session.commit()
    return var_w, var_l


def ensure_catalog_fixes():
    """Corrige le catalogue existant (prix Coreff Ambrée + placeholders)."""
    prod = Product.query.filter(func.lower(Product.name) == "coreff ambrée").first()
    if prod:
        v = Variant.query.filter_by(product_id=prod.id, size_l=22).first()
        if v and (v.price_ttc is None or float(v.price_ttc) != 78.0):
            v.price_ttc = 78.0
            db.session.add(v)
            db.session.commit()
    get_or_create_equipment_placeholder()
    get_or_create_ecocup_variants()


def ensure_data_consistency():
    """Évite les NULL anciens qui font planter les calculs."""
    db.session.execute(
        update(Movement).where(Movement.unit_price_ttc.is_(None)).values(unit_price_ttc=0.0)
    )
    db.session.execute(
        update(Movement).where(Movement.deposit_per_keg.is_(None)).values(deposit_per_keg=0.0)
    )
    db.session.commit()


# --------- Encodage / Décodage matériel ----------
def pack_equipment(notes_text, eq_dict):
    clean = (notes_text or "").strip()
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


def sum_equipment_for_client(client_id: int):
    """Somme nette du matériel chez le client (OUT +, IN/DEFECT -)."""
    totals = {k: 0 for k in EQ_KEYS}
    movements = Movement.query.filter_by(client_id=client_id).all()
    for m in movements:
        eq, _ = unpack_equipment(m.notes)
        sign = 1 if m.type == 'OUT' else -1
        for k in EQ_KEYS:
            totals[k] += sign * int(eq.get(k, 0) or 0)
    for k in EQ_KEYS:
        if totals[k] < 0:
            totals[k] = 0
    return totals


# --------- Expressions SQL réutilisables ----------
BEER_VALUE_EXPR = case(
    (Movement.type == 'OUT', Movement.qty * func.coalesce(Movement.unit_price_ttc, 0.0)),
    (Movement.type == 'DEFECT', -Movement.qty * func.coalesce(Movement.unit_price_ttc, 0.0)),
    else_=0.0
)
DEPOSIT_VALUE_EXPR = case(
    (Movement.type == 'OUT', Movement.qty * func.coalesce(Movement.deposit_per_keg, 0.0)),
    (Movement.type == 'IN', -Movement.qty * func.coalesce(Movement.deposit_per_keg, 0.0)),
    (Movement.type == 'DEFECT', -Movement.qty * func.coalesce(Movement.deposit_per_keg, 0.0)),
    else_=0.0
)
STOCK_EXPR = case(
    (Movement.type == 'OUT', Movement.qty),
    (Movement.type == 'IN', -Movement.qty),
    (Movement.type == 'DEFECT', -Movement.qty),
    else_=0
)


# --------- App Factory ----------
def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'devkey')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///data.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()
        seed_if_empty()
        ensure_catalog_fixes()
        ensure_data_consistency()

    @app.errorhandler(404)
    def not_found(e): return render_template('404.html'), 404

    @app.errorhandler(500)
    def server_err(e): return render_template('500.html'), 500

    # --------- Routes ----------
    @app.route('/')
    def index():
        # IMPORTANT: on ignore les variants 0L pour le stock fûts
        last_delivery = func.max(case((Movement.type == 'OUT', Movement.created_at), else_=None))
        last_pickup   = func.max(case((or_(Movement.type == 'IN', Movement.type == 'DEFECT'), Movement.created_at), else_=None))

        total_delivered_qty = func.coalesce(func.sum(case(
            (and_(Movement.type == 'OUT', Variant.size_l > 0), Movement.qty)
        , else_=0)), 0)

        rows = db.session.query(
            Client.id, Client.name,
            func.coalesce(func.sum(case((and_(Movement.type == 'OUT', Variant.size_l > 0), Movement.qty), else_=0)), 0).label('out_qty'),
            func.coalesce(func.sum(case((and_(or_(Movement.type == 'IN', Movement.type == 'DEFECT'), Variant.size_l > 0), Movement.qty), else_=0)), 0).label('in_qty'),
            func.coalesce(func.sum(DEPOSIT_VALUE_EXPR), 0.0).label('deposit_in_play'),
            func.coalesce(func.sum(BEER_VALUE_EXPR), 0.0).label('beer_billed_cum'),
            last_delivery.label('last_delivery_at'),
            last_pickup.label('last_pickup_at'),
            total_delivered_qty.label('delivered_qty_cum'),
            func.coalesce(func.sum(BEER_VALUE_EXPR), 0.0).label('beer_billed_total')
        ).join(Movement, Movement.client_id == Client.id, isouter=True) \
         .join(Variant, Variant.id == Movement.variant_id, isouter=True) \
         .group_by(Client.id, Client.name) \
         .order_by(Client.name).all()

        # Matériel prêté par client (inclut Ecocup)
        equipment_by_client = {c.id: {k: 0 for k in EQ_KEYS} for c in Client.query.all()}
        for m in Movement.query.with_entities(Movement.client_id, Movement.type, Movement.notes).all():
            if m.client_id is None:
                continue
            eq, _ = unpack_equipment(m.notes)
            sign = 1 if m.type == 'OUT' else -1  # DEFECT agit comme IN côté matériel
            bucket = equipment_by_client.setdefault(m.client_id, {k: 0 for k in EQ_KEYS})
            for k in EQ_KEYS:
                bucket[k] += sign * int(eq.get(k, 0) or 0)
        for cid, d in equipment_by_client.items():
            for k in EQ_KEYS:
                if d[k] < 0: d[k] = 0

        return render_template('index.html', rows=rows, equipment_by_client=equipment_by_client)

    @app.route('/clients', methods=['GET', 'POST'])
    def clients():
        if request.method == 'POST':
            name = (request.form.get('name') or '').strip()
            if not name:
                flash("Nom de client requis.", "danger")
                return redirect(url_for('clients'))
            exists = Client.query.filter(func.lower(Client.name) == name.lower()).first()
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

        # Historique détaillé & évènements Ecocup
        movements = Movement.query.filter_by(client_id=client_id).order_by(Movement.created_at.desc()).all()
        display_movs = []
        ecocup_events = []
        for m in movements:
            eq, human = unpack_equipment(m.notes)
            display_movs.append((m, eq, human))
            if (eq.get('ecocup') or 0) != 0:
                ecocup_events.append({
                    "date": m.created_at,
                    "type": m.type,
                    "qty": int(eq.get('ecocup') or 0),
                    "note": human
                })

        # Totaux
        beer_billed_cum = db.session.query(func.coalesce(func.sum(BEER_VALUE_EXPR), 0.0)) \
                                    .filter(Movement.client_id == client_id).scalar()
        deposit_in_play = db.session.query(func.coalesce(func.sum(DEPOSIT_VALUE_EXPR), 0.0)) \
                                    .filter(Movement.client_id == client_id).scalar()
        delivered_qty_cum = db.session.query(
            func.coalesce(func.sum(case((and_(Movement.type == 'OUT', Variant.size_l > 0), Movement.qty), else_=0)), 0)
        ).join(Variant, Variant.id == Movement.variant_id) \
         .filter(Movement.client_id == client_id).scalar()
        in_place_total = db.session.query(func.coalesce(func.sum(
            case(
                (and_(Movement.type == 'OUT', Variant.size_l > 0), Movement.qty),
                (and_(or_(Movement.type == 'IN', Movement.type == 'DEFECT'), Variant.size_l > 0), -Movement.qty),
                else_=0
            )
        ), 0)).join(Variant, Variant.id == Movement.variant_id) \
         .filter(Movement.client_id == client_id).scalar()

        equipment_totals = sum_equipment_for_client(client_id)

        last_delivery_at = db.session.query(func.max(Movement.created_at)) \
            .filter(Movement.client_id == client_id, Movement.type == 'OUT').scalar()
        last_pickup_at = db.session.query(func.max(Movement.created_at)) \
            .filter(Movement.client_id == client_id, or_(Movement.type == 'IN', Movement.type == 'DEFECT')).scalar()

        return render_template('client_detail.html',
                               client=client, rows=rows, movements=display_movs,
                               ecocup_events=ecocup_events,
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
                return redirect(url_for('movement_new'))

            # Produit/variant: catalogue, hors catalogue, ou matériel seul
            custom_name = (request.form.get('custom_name') or '').strip()
            variant_id = None
            variant = None

            if custom_name:
                custom_size = int(request.form.get('custom_size_l', '0') or 0)
                if custom_size <= 0:
                    flash("Format (L) invalide pour la référence hors catalogue.", "danger")
                    return redirect(url_for('movement_new', client_id=client_id))
                unit_price_raw = (request.form.get('unit_price_ttc') or '').strip()
                unit_price = float(unit_price_raw) if unit_price_raw else 0.0
                product = Product.query.filter(func.lower(Product.name) == custom_name.lower()).first()
                if not product:
                    product = Product(name=custom_name)
                    db.session.add(product)
                    db.session.flush()
                variant = Variant.query.filter_by(product_id=product.id, size_l=custom_size).first()
                if not variant:
                    variant = Variant(product_id=product.id, size_l=custom_size, price_ttc=unit_price)
                    db.session.add(variant)
                    db.session.flush()
                else:
                    if unit_price and (variant.price_ttc != unit_price):
                        variant.price_ttc = unit_price
                        db.session.flush()
                variant_id = variant.id
            else:
                variant_id_field = request.form.get('variant_id')
                if variant_id_field:
                    variant_id = int(variant_id_field) if variant_id_field.strip() else None
                if variant_id:
                    variant = Variant.query.get_or_404(variant_id)

            # Matériel / Ecocup
            eq = {
                "tireuse": int(request.form.get('eq_tireuse', 0) or 0),
                "co2": int(request.form.get('eq_co2', 0) or 0),
                "comptoir": int(request.form.get('eq_comptoir', 0) or 0),
                "tonnelle": int(request.form.get('eq_tonnelle', 0) or 0),
                "ecocup": int(request.form.get('eq_ecocup', 0) or 0),
            }
            any_equipment = any(v > 0 for v in eq.values())

            # Si ni produit ni matériel -> erreur
            if not variant_id and not any_equipment:
                flash("Choisis une référence ou saisis du matériel prêté/repris.", "danger")
                return redirect(url_for('movement_new', client_id=client_id))

            # Si matériel seul -> placeholder "Matériel seul (0L)"
            if any_equipment and not variant_id:
                variant = get_or_create_equipment_placeholder()
                variant_id = variant.id

            # Quantité / Type / Date
            qty = int(request.form.get('qty', 0))   # autorise 0
            mtype = request.form['type']            # 'OUT' | 'IN' | 'DEFECT'
            created_at = datetime.utcnow()
            date_str = (request.form.get('when') or '').strip()
            if date_str:
                try:
                    created_at = datetime.strptime(date_str, "%Y-%m-%dT%H:%M")
                except ValueError:
                    flash("Format de date invalide.", "danger")
                    return redirect(url_for('movement_new', client_id=client_id))

            # Prix / Consigne
            unit_price_raw = (request.form.get('unit_price_ttc') or '').strip()
            deposit_per_keg = float(request.form.get('deposit_per_keg', 30) or 30)

            if qty == 0:
                unit_price = 0.0
                deposit_per_keg = 0.0
            else:
                if custom_name:
                    unit_price = float(unit_price_raw) if unit_price_raw else unit_price  # défini plus haut
                else:
                    unit_price = float(unit_price_raw) if unit_price_raw else (variant.price_ttc if variant and variant.price_ttc is not None else 0.0)

            # --- Validations anti-négatif pour fûts et matériel (hors Ecocup géré à part) ---
            if mtype in ('IN', 'DEFECT'):
                if qty > 0:
                    in_place_variant = db.session.query(func.coalesce(func.sum(STOCK_EXPR), 0)) \
                        .join(Variant, Variant.id == Movement.variant_id) \
                        .filter(and_(Movement.client_id == client_id, Movement.variant_id == variant_id, Variant.size_l > 0)).scalar()
                    if qty > max(in_place_variant, 0):
                        flash(f"Impossible de reprendre {qty} fût(s) — stock dispo: {max(in_place_variant, 0)}.", "danger")
                        return redirect(url_for('movement_new', client_id=client_id))
                if any_equipment:
                    current_eq = sum_equipment_for_client(client_id)
                    for k in EQ_KEYS:
                        if k == "ecocup":
                            continue  # géré plus bas
                        want = int(eq.get(k, 0) or 0)
                        if want > current_eq.get(k, 0):
                            flash(f"Impossible de reprendre {want} {k}(s) — dispo: {current_eq.get(k, 0)}.", "danger")
                            return redirect(url_for('movement_new', client_id=client_id))

            # Enregistrement principal
            human_notes = (request.form.get('notes', '').strip() or "")
            notes = pack_equipment(human_notes, eq)

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

            # --- Règles Ecocup (NOUVELLES) ---
            var_wash, var_loss = get_or_create_ecocup_variants()

            if mtype == 'OUT':
                # Plus de facturation lavage au prêt (règle modifiée) — rien à faire ici.
                pass

            if mtype in ('IN', 'DEFECT'):
                # 1) On calcule ce que le client a AVANT ce retour
                current_ec = sum_equipment_for_client(client_id).get("ecocup", 0)

                # 2) Récupéré (retourné) ce jour
                returned = eq.get("ecocup", 0) or 0
                if returned > current_ec:
                    returned = current_ec  # clamp sécurité

                # 3) Manquants
                missing = max(0, current_ec - returned)

                # 4) Facturer lavage sur les gobelets récupérés
                if returned > 0:
                    db.session.add(Movement(
                        created_at=created_at,
                        type='OUT',
                        client_id=client_id,
                        variant_id=var_wash.id,
                        qty=returned,
                        unit_price_ttc=ECOCUP_WASH_PRICE,
                        deposit_per_keg=0.0,
                        notes=f"Lavage Ecocup {returned}u (lié au mouvement #{main_m.id})"
                    ))

                # 5) Facturer perte sur les manquants
                if missing > 0:
                    db.session.add(Movement(
                        created_at=created_at,
                        type='OUT',
                        client_id=client_id,
                        variant_id=var_loss.id,
                        qty=missing,
                        unit_price_ttc=ECOCUP_LOSS_PRICE,
                        deposit_per_keg=0.0,
                        notes=f"Ecocup manquant {missing}u (lié au mouvement #{main_m.id})"
                    ))

                # 6) Ajuster la note du mouvement principal pour tracer totals retirés
                if (returned + missing) > 0:
                    eq_adj = dict(eq)
                    eq_adj["ecocup"] = returned + missing
                    extra = []
                    if returned > 0: extra.append(f"{returned} lavés")
                    if missing > 0: extra.append(f"{missing} manquants")
                    extra_txt = " — " + ", ".join(extra) if extra else ""
                    main_m.notes = pack_equipment(human_notes + extra_txt, eq_adj)

            db.session.commit()
            flash('Mouvement enregistré ✅', "success")
            return redirect(url_for('client_detail', client_id=client_id))

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


app = create_app()
