# models.py — version SANS ecocup/gobelets

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Client(db.Model):
    __tablename__ = "client"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

    def __repr__(self):
        return f"<Client {self.name}>"


class Product(db.Model):
    __tablename__ = "product"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)

    def __repr__(self):
        return f"<Product {self.name}>"


class Variant(db.Model):
    __tablename__ = "variant"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    volume_l = db.Column(db.Integer, nullable=False)  # ex: 20, 22, 30
    deposit_eur = db.Column(db.Integer, default=0)

    product = db.relationship("Product", backref="variants")

    def __repr__(self):
        return f"<Variant {self.product.name} {self.volume_l}L>"


class Movement(db.Model):
    __tablename__ = "movement"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    variant_id = db.Column(db.Integer, db.ForeignKey("variant.id"), nullable=False)

    # "OUT" = livraison ; "IN" = reprise
    type = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship("Client", backref="movements")
    variant = db.relationship("Variant", backref="movements")

    def __repr__(self):
        return f"<Movement {self.type} x{self.quantity} {self.variant} -> {self.client}>"
