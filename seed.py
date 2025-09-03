# seed.py — initialisation des données (sans import de app !)
from models import db, Client, Product, Variant, Inventory, ReorderRule

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
    ("Coreff Blonde", 30, 5),  # mini 5 fûts
    ("Coreff Blonde", 20, 2),  # mini 2 fûts
]


def seed_if_empty():
    # Clients
    if db.session.query(Client).count() == 0:
        for name in DEFAULT_CLIENTS:
            db.session.add(Client(name=name))
        db.session.commit()

    # Produits + Variantes
    if db.session.query(Product).count() == 0:
        for pname, sizes in DEFAULT_CATALOG:
            p = Product(name=pname)
            db.session.add(p)
            db.session.flush()
            for size in sizes:
                db.session.add(Variant(product_id=p.id, size_l=size, price_ttc=None))
        db.session.commit()

    # Inventaire pour chaque variante
    for v in Variant.query.all():
        if not Inventory.query.filter_by(variant_id=v.id).first():
            db.session.add(Inventory(variant_id=v.id, qty=0))
    db.session.commit()

    # Seuils de réassort par défaut
    for pname, size, minq in REORDER_DEFAULTS:
        v = (
            db.session.query(Variant)
            .join(Product, Variant.product_id == Product.id)
            .filter(Product.name == pname, Variant.size_l == size)
            .first()
        )
        if v:
            r = ReorderRule.query.filter_by(variant_id=v.id).first()
            if not r:
                db.session.add(ReorderRule(variant_id=v.id, min_qty=minq))
            else:
                r.min_qty = minq
    db.session.commit()
