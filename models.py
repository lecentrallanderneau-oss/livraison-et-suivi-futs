from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Client(db.Model):
    __tablename__ = "clients"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)

    movements = db.relationship("Movement", backref="client", lazy=True)

    def __repr__(self):
        return f"<Client {self.id} {self.name!r}>"


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)

    variants = db.relationship("Variant", backref="product", lazy=True)

    def __repr__(self):
        return f"<Product {self.id} {self.name!r}>"


class Variant(db.Model):
    __tablename__ = "variants"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    size_l = db.Column(db.Integer, nullable=False)
    # Prix catalogue: peut être NULL, on gère côté app avec coalesce
    price_ttc = db.Column(db.Float, nullable=True)

    movements = db.relationship("Movement", backref="variant", lazy=True)

    def __repr__(self):
        return f"<Variant {self.id} prod={self.product_id} {self.size_l}L price={self.price_ttc}>"


class Movement(db.Model):
    __tablename__ = "movements"
    id = db.Column(db.Integer, primary_key=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    # IMPORTANT: était Enum('OUT','IN'). On passe en String(10) pour accepter 'DEFECT'
    type = db.Column(db.String(10), nullable=False, index=True)  # 'OUT' | 'IN' | 'DEFECT'

    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False, index=True)

    qty = db.Column(db.Integer, nullable=False, default=0)

    # Valorisation saisie au moment du mouvement. Peut être NULL sur anciens enregistrements.
    unit_price_ttc = db.Column(db.Float, nullable=True)
    deposit_per_keg = db.Column(db.Float, nullable=True)

    # Notes libres + encodage matériel (||EQ|tireuse=1;co2=0;comptoir=0;tonnelle=0)
    notes = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<Movement {self.id} {self.type} c={self.client_id} v={self.variant_id} qty={self.qty}>"
