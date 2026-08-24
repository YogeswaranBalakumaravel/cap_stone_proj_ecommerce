from datetime import date

from app.extensions import db
from app.models import Phone


def test_create_phone_with_defaults(app):
    with app.app_context():
        phone = Phone(
            brand="Apple",
            model_name="Test Phone",
            screen_size_in=6.1,
            chip="Test Chip",
            ram_gb=8,
            storage_options_gb="128,256",
        )
        db.session.add(phone)
        db.session.commit()

        saved = Phone.query.filter_by(model_name="Test Phone").first()
        assert saved is not None
        assert saved.tier == "Standard"
        assert saved.is_current is True
        assert isinstance(saved.release_date, date)


def test_storage_options_list(app):
    with app.app_context():
        phone = Phone(
            brand="Samsung",
            model_name="Storage Test",
            screen_size_in=6.7,
            chip="Test Chip",
            ram_gb=12,
            storage_options_gb="256, 512 ,1024",
        )
        assert phone.storage_options_list == ["256", "512", "1024"]


def test_to_dict_shape(app):
    with app.app_context():
        phone = Phone(
            brand="Apple",
            model_name="Dict Test",
            tier="Pro",
            screen_size_in=6.3,
            chip="A19 Pro",
            ram_gb=8,
            storage_options_gb="256,512",
            price_usd=1099.0,
        )
        db.session.add(phone)
        db.session.commit()

        data = phone.to_dict()
        assert data["brand"] == "Apple"
        assert data["model_name"] == "Dict Test"
        assert data["storage_options_gb"] == ["256", "512"]
        assert data["price_usd"] == 1099.0


def test_storage_options_list_empty_string(app):
    with app.app_context():
        phone = Phone(
            brand="Apple",
            model_name="No Storage Test",
            screen_size_in=6.1,
            chip="Test Chip",
            ram_gb=8,
            storage_options_gb="",
        )
        assert phone.storage_options_list == []


def test_to_dict_handles_missing_release_date(app):
    with app.app_context():
        phone = Phone(
            brand="Samsung",
            model_name="No Date Test",
            screen_size_in=6.2,
            chip="Test Chip",
            ram_gb=12,
            storage_options_gb="128",
            release_date=None,
        )
        data = phone.to_dict()
        assert data["release_date"] is None


def test_repr_includes_brand_and_model_name(app):
    with app.app_context():
        phone = Phone(
            brand="Apple",
            model_name="Repr Test",
            screen_size_in=6.1,
            chip="Test Chip",
            ram_gb=8,
            storage_options_gb="128",
        )
        assert repr(phone) == "<Phone Apple Repr Test>"
