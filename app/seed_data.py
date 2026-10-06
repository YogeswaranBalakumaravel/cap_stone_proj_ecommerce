"""Seed data for current Apple and Samsung flagship phones (as of Sep 2026),
and Apple's current AirPods lineup (as of Oct 2026).

Specs/prices change frequently -- treat this as a starting point to edit,
not a maintained live feed. The foldable iPhone Duo goes on sale Oct 23, 2026
and is intentionally not seeded here yet (upcoming, not current).
"""
from datetime import date

from .extensions import db
from .models import Earbud, Phone

PHONES = [
    # --- Apple ---
    dict(
        brand="Apple",
        model_name="iPhone 17",
        tier="Standard",
        release_date=date(2025, 9, 19),
        price_usd=899.0,
        screen_size_in=6.3,
        chip="A19",
        ram_gb=8,
        storage_options_gb="256,512",
        camera_summary="48MP Fusion main + 48MP ultra-wide, 2x optical-quality zoom",
        image_url="images/apple-phone.svg",
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="iPhone Air",
        tier="Air",
        release_date=date(2025, 9, 19),
        price_usd=1099.0,
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
        model_name="iPhone 18 Pro",
        tier="Pro",
        release_date=date(2026, 9, 18),
        price_usd=1199.0,
        screen_size_in=6.3,
        chip="A20 Pro",
        ram_gb=12,
        storage_options_gb="256,512,1024,2048",
        camera_summary="48MP variable-aperture main, 48MP ultra-wide, 48MP 4x tele, ProRes video",
        image_url="images/apple-phone.svg",
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="iPhone 18 Pro Max",
        tier="Pro Max",
        release_date=date(2026, 9, 18),
        price_usd=1299.0,
        screen_size_in=6.9,
        chip="A20 Pro",
        ram_gb=12,
        storage_options_gb="256,512,1024,2048",
        camera_summary="48MP variable-aperture main, 48MP ultra-wide, 48MP 4x tele, bigger battery",
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


# Apple's AirPods lineup after the Sep 2026 refresh: AirPods 5 replaced AirPods 4
# and AirPods 4 (ANC), both discontinued Sep 9, 2026. Battery hours are with ANC on.
# AirPods Max 2 are over-ear headphones with no charging case, so both battery
# fields hold the same single-charge figure.
EARBUDS = [
    dict(
        brand="Apple",
        model_name="AirPods 5",
        tier="Standard",
        release_date=date(2026, 9, 18),
        price_usd=129.0,
        chip="H2",
        battery_hours_earbuds=4.0,
        battery_hours_with_case=20.0,
        noise_cancellation=True,
        water_resistance_rating="IP57",
        connector_type="USB-C",
        image_url="images/apple-earbuds.svg",
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="AirPods 5 with Wireless Charging Case",
        tier="Standard",
        release_date=date(2026, 9, 18),
        price_usd=149.0,
        chip="H2",
        battery_hours_earbuds=5.0,
        battery_hours_with_case=22.0,
        noise_cancellation=True,
        water_resistance_rating="IP57",
        connector_type="USB-C",
        image_url="images/apple-earbuds.svg",
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="AirPods Pro 3",
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
        is_current=True,
    ),
    dict(
        brand="Apple",
        model_name="AirPods Max 2",
        tier="Max",
        release_date=date(2026, 4, 1),
        price_usd=549.0,
        chip="H2",
        battery_hours_earbuds=20.0,
        battery_hours_with_case=20.0,
        noise_cancellation=True,
        water_resistance_rating="",
        connector_type="USB-C",
        image_url="images/apple-earbuds.svg",
        is_current=True,
    ),
]


def seed_if_empty():
    """Populate each catalog table from its seed list if that table is currently empty.

    The tables are checked separately, so a database that already holds phones
    still gets the earbuds seeded on its next boot.
    """
    if Phone.query.first() is None:
        db.session.add_all(Phone(**data) for data in PHONES)
    if Earbud.query.first() is None:
        db.session.add_all(Earbud(**data) for data in EARBUDS)
    db.session.commit()
