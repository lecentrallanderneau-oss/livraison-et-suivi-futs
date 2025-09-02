from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

class Variant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    size_l = db.Column(db.Integer, nullable=False)           # 20, 22, 30…
    price_ttc = db.Column(db.Float, nullable=True)           # EUR TTC (facultatif)
    product = db.relationship('Product', backref='variants')

class Movement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    type = db.Column(db.String(3), nullable=False)           # 'OUT' ou 'IN'
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    variant_id = db.Column(db.Integer, db.ForeignKey('variant.id'), nullable=False)
    qty = db.Column(db.Integer, default=1, nullable=False)
    unit_price_ttc = db.Column(db.Float, nullable=True)      # prérempli selon Variant
    deposit_per_keg = db.Column(db.Float, default=30.0, nullable=False)
    notes = db.Column(db.String(280), nullable=True)

    client = db.relationship('Client', backref='movements')
    variant = db.relationship('Variant')
