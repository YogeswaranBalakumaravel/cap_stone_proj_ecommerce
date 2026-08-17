"""Seed data for current Apple and Samsung flagship phones (as of Aug 2026).

Specs/prices change frequently -- treat this as a starting point to edit,
not a maintained live feed. iPhone 18 Pro / Pro Max are expected fall 2026
and are intentionally not seeded here (upcoming, not current).
"""
from datetime import date

from .extensions import db
from .models import Phone

PHONES = [
    # --- Apple ---
    dict(
        brand="Apple",
        model_name="iPhone 17",
        tier="Standard",
        release_date=date(2025, 9, 19),
        price_usd=799.0,
        screen_size_in=6.3,
        chip="A19",
        ram_gb=8,
        storage_options_gb="128,256,512",
        camera_summary="48MP Fusion main + 48MP ultra-wide, 2x optical-quality zoom",
        image_url="images/apple-phone.svg",
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="iPhone Air",
        tier="Air",
        release_date=date(2025, 9, 19),
        price_usd=999.0,
        screen_size_in=6.5,
        chip="A19 Pro",
        ram_gb=8,
        storage_options_gb="256,512,1024",
        camera_summary="48MP single-lens Fusion camera in an ultra-thin titanium body",
        image_url="images/apple-phone.svg",
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="iPhone 17 Pro",
        tier="Pro",
        release_date=date(2025, 9, 19),
        price_usd=1099.0,
        screen_size_in=6.3,
        chip="A19 Pro",
        ram_gb=8,
        storage_options_gb="256,512,1024",
        camera_summary="48MP Fusion main, 48MP ultra-wide, 48MP 4x tele, ProRes video",
        image_url="images/apple-phone.svg",
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="iPhone 17 Pro Max",
        tier="Pro Max",
        release_date=date(2025, 9, 19),
        price_usd=1199.0,
        screen_size_in=6.9,
        chip="A19 Pro",
        ram_gb=8,
        storage_options_gb="256,512,1024,2048",
        camera_summary="48MP Fusion main, 48MP ultra-wide, 48MP 4x tele, largest battery",
        image_url="images/apple-phone.svg",
        is_current=True,
    ),
    # --- Samsung ---
    dict(
        brand="Samsung",
        model_name="Galaxy S26",
        tier="Standard",
        release_date=date(2026, 2, 25),
        price_usd=799.0,
        screen_size_in=6.2,
        chip="Snapdragon 8 Elite Gen 5",
        ram_gb=12,
        storage_options_gb="128,256",
        camera_summary="50MP wide + 12MP ultra-wide + 10MP 3x tele, Galaxy AI",
        image_url="images/samsung-phone.svg",
        is_current=True,
    ),
    dict(
        brand="Samsung",
        model_name="Galaxy S26+",
        tier="Plus",
        release_date=date(2026, 2, 25),
        price_usd=999.0,
        screen_size_in=6.7,
        chip="Snapdragon 8 Elite Gen 5",
        ram_gb=12,
        storage_options_gb="256,512",
        camera_summary="50MP wide + 12MP ultra-wide + 10MP 3x tele, larger battery",
        image_url="images/samsung-phone.svg",
        is_current=True,
    ),
    dict(
        brand="Samsung",
        model_name="Galaxy S26 Ultra",
        tier="Ultra",
        release_date=date(2026, 2, 25),
        price_usd=1299.0,
        screen_size_in=6.9,
        chip="Snapdragon 8 Elite Gen 5",
        ram_gb=12,
        storage_options_gb="256,512,1024",
        camera_summary="200MP wide + 50MP 5x periscope tele + 10MP 3x tele + built-in S Pen",
        image_url="images/samsung-phone.svg",
        is_current=True,
    ),
]


def seed_if_empty():
    """Populate the phones table from PHONES if it's currently empty."""
    if Phone.query.first() is not None:
        return
    db.session.add_all(Phone(**data) for data in PHONES)
    db.session.commit()
