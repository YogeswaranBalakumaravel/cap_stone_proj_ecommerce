from datetime import date

from app.extensions import db
from app.models import Phone
from app.seed_data import PHONES, seed_if_empty


def test_seed_if_empty_populates_table_on_first_run(app):
    # create_app() already calls seed_if_empty() once during the `app`
    # fixture's setup, so the table should already match PHONES.
    with app.app_context():
        assert Phone.query.count() == len(PHONES)


def test_seed_if_empty_is_idempotent(app):
    with app.app_context():
        seed_if_empty()
        seed_if_empty()
        assert Phone.query.count() == len(PHONES)


def test_seed_if_empty_does_not_overwrite_existing_data(app):
    with app.app_context():
        db.session.query(Phone).delete()
        db.session.commit()
        db.session.add(Phone(brand="Apple", model_name="Custom Only"))
        db.session.commit()

        seed_if_empty()

        # Table wasn't empty, so seed_if_empty should have been a no-op.
        assert Phone.query.count() == 1
        assert Phone.query.first().model_name == "Custom Only"


def test_seed_data_includes_both_brands(app):
    with app.app_context():
        brands = {p.brand for p in Phone.query.all()}
        assert brands == {"Apple", "Samsung"}


def test_seed_data_entries_are_all_current():
    assert all(p["is_current"] for p in PHONES)


def test_seed_data_has_no_duplicate_phones():
    keys = [(p["brand"], p["model_name"]) for p in PHONES]
    assert len(keys) == len(set(keys))


def test_seed_data_storage_tiers_are_ascending_whole_gb():
    # Catch typos like "1TB", or out-of-order or repeated tiers, in the hand-edited lists.
    for p in PHONES:
        tiers = p["storage_options_gb"].split(",")
        assert all(t.isdigit() for t in tiers), p["model_name"]
        sizes = [int(t) for t in tiers]
        assert sizes == sorted(set(sizes)), p["model_name"]


def test_seed_data_has_no_unreleased_phones():
    # Upcoming phones (e.g. the iPhone Duo, on sale Oct 23, 2026) aren't seeded
    # as current until they're on sale.
    unreleased = [p["model_name"] for p in PHONES if p["release_date"] > date.today()]
    assert unreleased == []
