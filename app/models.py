"""SQLAlchemy models."""
from datetime import date

from .extensions import db


class Phone(db.Model):
    __tablename__ = "phones"

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(20), nullable=False)
    model_name = db.Column(db.String(80), nullable=False)
    tier = db.Column(db.String(20), nullable=False, default="Standard")
    release_date = db.Column(db.Date, nullable=False, default=date.today)
    price_usd = db.Column(db.Float, nullable=False, default=0.0)
    screen_size_in = db.Column(db.Float, nullable=False, default=0.0)
    chip = db.Column(db.String(60), nullable=False, default="")
    ram_gb = db.Column(db.Integer, nullable=False, default=0)
    storage_options_gb = db.Column(db.String(40), nullable=False, default="")
    camera_summary = db.Column(db.String(200), nullable=False, default="")
    image_url = db.Column(db.String(200), nullable=False, default="")
    is_current = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def storage_options_list(self):
        """Storage tiers as a list of strings, e.g. ["256", "512", "1024"]."""
        return [s.strip() for s in self.storage_options_gb.split(",") if s.strip()]

    def to_dict(self):
        return {
            "id": self.id,
            "brand": self.brand,
            "model_name": self.model_name,
            "tier": self.tier,
            "release_date": self.release_date.isoformat() if self.release_date else None,
            "price_usd": self.price_usd,
            "screen_size_in": self.screen_size_in,
            "chip": self.chip,
            "ram_gb": self.ram_gb,
            "storage_options_gb": self.storage_options_list,
            "camera_summary": self.camera_summary,
            "image_url": self.image_url,
            "is_current": self.is_current,
        }

    def __repr__(self):
        return f"<Phone {self.brand} {self.model_name}>"
