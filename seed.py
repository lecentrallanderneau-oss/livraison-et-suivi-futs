# seed.py — script simple pour (re)créer la base et l'ensemencer

from app import app, db, DEFAULT_CLIENTS, DEFAULT_PRODUCTS, Client, Product, Variant

with app.app_context():
    db.drop_all()
    db.create_all()

    for name in DEFAULT_CLIENTS:
        db.session.add(Client(name=name))

    for pname, vols in DEFAULT_PRODUCTS:
        p = Product(name=pname)
        db.session.add(p)
        db.session.flush()
        for vol in vols:
            db.session.add(Variant(product_id=p.id, volume_l=vol, deposit_eur=0))

    db.session.commit()
    print("✔ Base recréée et peuplée.")
