import os
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ----------------------------
# Modèles existants (inchangés)
# ----------------------------
class Client(db.Model):
    __tablename__ = "clients"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    email = db.Column(db.String(255), nullable=True)

    # relations utiles
    movements = db.relationship("Movement", backref="client", lazy="dynamic")
    equipments = db.relationship("EquipmentLoan", backref="client", lazy="dynamic")
    ecocup_ops = db.relationship("EcocupOperation", backref="client", lazy="dynamic")  # <-- new

    def __repr__(self):
        return f"<Client {self.name}>"


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)

    variants = db.relationship("Variant", backref="product", lazy="dynamic")

    def __repr__(self):
        return f"<Product {self.name}>"


class Variant(db.Model):
    __tablename__ = "variants"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    capacity_l = db.Column(db.Float, nullable=True)  # peut être null si non pertinent

    # inventaire lié
    inventory = db.relationship("Inventory", backref="variant", uselist=False)

    def __repr__(self):
        return f"<Variant {self.name} of {self.product.name}>"


class Movement(db.Model):
    """
    Mouvements fûts (OUT livraison / IN reprise / DEFECT / FULL etc.)
    """
    __tablename__ = "movements"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # OUT, IN, DEFECT, FULL
    qty = db.Column(db.Integer, nullable=False, default=0)
    unit_price_ttc = db.Column(db.Float, nullable=False, default=0.0)
    deposit_per_keg = db.Column(db.Float, nullable=False, default=0.0)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    variant = db.relationship("Variant")

    def __repr__(self):
        return f"<Movement {self.type} {self.qty}>"


class Inventory(db.Model):
    __tablename__ = "inventory"
    id = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False, unique=True)
    qty = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<Inventory v#{self.variant_id} qty={self.qty}>"


class ReorderRule(db.Model):
    __tablename__ = "reorder_rules"
    id = db.Column(db.Integer, primary_key=True)
    variant_id = db.Column(db.Integer, db.ForeignKey("variants.id"), nullable=False, unique=True)
    min_qty = db.Column(db.Integer, nullable=False, default=0)

    variant = db.relationship("Variant")

    def __repr__(self):
        return f"<ReorderRule v#{self.variant_id} min={self.min_qty}>"


class EquipmentLoan(db.Model):
    """
    Prêt de matériel (existant dans l’app).
    """
    __tablename__ = "equipment_loans"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    item_name = db.Column(db.String(255), nullable=False)
    qty = db.Column(db.Integer, nullable=False, default=0)
    out_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    in_date = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<EquipmentLoan {self.item_name} x{self.qty}>"


# --------------------------------
# NOUVEAU : Éco-cups (opérations)
# --------------------------------
class EcocupOperation(db.Model):
    """
    Une opération compacte “Éco-cups” couvrant un prêt et son retour associé,
    pour facturer simplement :
      - 1,00 € par gobelet manquant (perdu)
      - 0,10 € de lavage par gobelet rendu

    On saisit : client, date, nb prêtés, nb rendus (+ tarifs par défaut modifiables).
    """
    __tablename__ = "ecocup_operations"
    id = db.Column(db.Integer, primary_key=True)

    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)

    op_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    qty_loaned = db.Column(db.Integer, nullable=False, default=0)
    qty_returned = db.Column(db.Integer, nullable=False, default=0)

    lost_fee_per = db.Column(db.Float, nullable=False, default=1.00)   # €/verre manquant
    wash_fee_per = db.Column(db.Float, nullable=False, default=0.10)  # €/verre rendu

    note = db.Column(db.Text, nullable=True)

    # ---- Calculs pratiques ----
    @property
    def lost_qty(self) -> int:
        return max((self.qty_loaned or 0) - (self.qty_returned or 0), 0)

    @property
    def lost_amount(self) -> float:
        return round(self.lost_qty * (self.lost_fee_per or 0.0), 2)

    @property
    def wash_amount(self) -> float:
        return round((self.qty_returned or 0) * (self.wash_fee_per or 0.0), 2)

    @property
    def total_amount(self) -> float:
        return round(self.lost_amount + self.wash_amount, 2)

    def __repr__(self):
        return f"<EcocupOp client#{self.client_id} loaned={self.qty_loaned} returned={self.qty_returned}>"
