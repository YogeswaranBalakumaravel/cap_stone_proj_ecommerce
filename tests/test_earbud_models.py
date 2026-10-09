from datetime import date

from app.extensions import db
from app.models import Earbud


def test_create_earbud_with_defaults(app):
    with app.app_context():
        db.session.add(Earbud(brand="Apple", model_name="Defaults Test"))
        db.session.commit()

        saved = Earbud.query.filter_by(model_name="Defaults Test").one()
        assert saved.tier == "Standard"
        assert saved.is_current is True
        assert saved.noise_cancellation is False
        assert saved.water_resistance_rating == ""
        assert saved.price_usd == 0.0
        assert isinstance(saved.release_date, date)


def test_to_dict_shape(app):
    with app.app_context():
        earbud = Earbud(
            brand="Apple",
            model_name="Dict Test",
            tier="Pro",
            release_date=date(2025, 9, 19),
            price_usd=249.0,
            chip="H2",
            battery_hours_earbuds=8.0,
            battery_hours_with_case=24.0,
            noise_cancellation=True,
            water_resistance_rating="IP57",
            connector_type="USB-C",
            image_url="images/apple-earbuds.svg",
            is_current=False,
        )
        db.session.add(earbud)
        db.session.commit()

        assert earbud.to_dict() == {
            "id": earbud.id,
            "brand": "Apple",
            "model_name": "Dict Test",
            "tier": "Pro",
            "release_date": "2025-09-19",
            "price_usd": 249.0,
            "chip": "H2",
            "battery_hours_earbuds": 8.0,
            "battery_hours_with_case": 24.0,
            "noise_cancellation": True,
            "water_resistance_rating": "IP57",
            "connector_type": "USB-C",
            "image_url": "images/apple-earbuds.svg",
            "is_current": False,
        }


def test_to_dict_handles_missing_release_date(app):
    with app.app_context():
        earbud = Earbud(brand="Apple", model_name="No Date Test", release_date=None)
        assert earbud.to_dict()["release_date"] is None


def test_repr_includes_brand_and_model_name(app):
    with app.app_context():
        assert repr(Earbud(brand="Apple", model_name="AirPods 5")) == "<Earbud Apple AirPods 5>"
