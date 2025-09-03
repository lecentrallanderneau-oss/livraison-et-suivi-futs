from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Client(db.Model):
    __tablename__ = "clients"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)

    movements = db.relationship("Movement", backref="client", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Client {self.id} {self.name!r}>"

class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)

    variants = db.relationship("Variant", backref="product", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Product {self.id} {self.name!r}>"

class Variant(db.Model):
    __tablename__ = "variants"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    size_l = db.Column(db.Integer, nullable=False)  # 20, 22, 30...
    price_ttc = db.Column(db.Float, nullable=True)

    movements = db.relationship("Movement", backref="variant", lazy=True)
    inventory = db.relationship("Inventory", backref="variant", uselist=False, cascade="all, delete-orphan")
    reorder_rule = db.relationship("ReorderRule", backref="variant", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint('product_id', 'size_l', name='uix_product_size'),
    )

    def __repr__(self):
        return f"<Variant {self.id} p={self.product_id} {self.size_l}L>"

class Movement(db.Model):
    __tablename__ = "movements"
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(10), nullable=False)  # 'OUT', 'IN', 'DEFECT'
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False, index=True)

    qty = db.Column(db.Integer, nullable=False, default=0)

    unit_price_ttc = db.Column(db.Float, nullable=True)
    deposit_per_keg = db.Column(db.Float, nullable=True)

    notes = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<Movement {self.id} {self.type} c={self.client_id} v={self.variant_id} qty={self.qty}>"

class Inventory(db.Model):
    """Stock bar (fûts pleins) par variante."""
    __tablename__ = "inventory"
    id = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False, unique=True, index=True)
    qty = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Inventory v={self.variant_id} qty={self.qty}>"

class ReorderRule(db.Model):
    """Seuil mini à maintenir par variante."""
    __tablename__ = "reorder_rules"
    id = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False, unique=True, index=True)
    min_qty = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<ReorderRule v={self.variant_id} min={self.min_qty}>"
