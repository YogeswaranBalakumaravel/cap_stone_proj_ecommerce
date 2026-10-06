from app.models import Earbud


def _earbud_id(app, model_name):
    with app.app_context():
        return Earbud.query.filter_by(model_name=model_name).one().id


def test_api_earbuds_apple_lineup_matches_apple_store(client):
    # Oct 2026: AirPods 5 (two cases), AirPods Pro 3 and AirPods Max 2.
    # Default sort: brand, then price descending.
    data = client.get("/api/earbuds?brand=Apple").get_json()
    assert [(e["model_name"], e["tier"], e["price_usd"]) for e in data] == [
        ("AirPods Max 2", "Max", 549.0),
        ("AirPods Pro 3", "Pro", 249.0),
        ("AirPods 5 with Wireless Charging Case", "Standard", 149.0),
        ("AirPods 5", "Standard", 129.0),
    ]


def test_api_earbuds_has_no_discontinued_airpods_4(client):
    names = {e["model_name"] for e in client.get("/api/earbuds").get_json()}
    assert not any(name.startswith("AirPods 4") for name in names)


def test_api_earbuds_every_current_airpods_model_has_anc_and_usb_c(client):
    for earbud in client.get("/api/earbuds?brand=Apple").get_json():
        assert earbud["noise_cancellation"] is True, earbud["model_name"]
        assert earbud["connector_type"] == "USB-C", earbud["model_name"]
        assert earbud["chip"] == "H2", earbud["model_name"]
        assert earbud["is_current"] is True, earbud["model_name"]


def test_api_earbuds_sort_by_price_is_ascending(client):
    prices = [e["price_usd"] for e in client.get("/api/earbuds?sort=price").get_json()]
    assert prices == [129.0, 149.0, 249.0, 549.0]


def test_api_earbuds_sort_by_release_date_is_newest_first(client):
    data = client.get("/api/earbuds?sort=release_date").get_json()
    dates = [e["release_date"] for e in data]
    assert dates == sorted(dates, reverse=True)
    assert {e["model_name"] for e in data[:2]} == {
        "AirPods 5",
        "AirPods 5 with Wireless Charging Case",
    }
    assert data[-1]["model_name"] == "AirPods Pro 3"


def test_api_earbuds_samsung_filter_returns_empty_list(client):
    # Only Apple earbuds are seeded so far; the Samsung filter is valid, just empty.
    resp = client.get("/api/earbuds?brand=Samsung")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_api_earbuds_ignores_invalid_brand_and_sort(client):
    data = client.get("/api/earbuds?brand=Sony&sort=popularity").get_json()
    # Unknown filter values fall back to the full catalog in the default order.
    assert [e["price_usd"] for e in data] == [549.0, 249.0, 149.0, 129.0]


def test_earbuds_page_lists_airpods_under_apple(client):
    resp = client.get("/earbuds?brand=Apple")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "4 models" in body
    assert "AirPods Pro 3" in body
    assert "AirPods Max 2" in body
    assert "$549" in body
    assert "iPhone" not in body


def test_earbuds_page_brand_tabs_stay_on_earbuds(client):
    body = client.get("/earbuds").get_data(as_text=True)
    assert 'href="/earbuds?brand=Apple"' in body
    assert 'href="/earbuds?brand=Samsung"' in body


def test_earbuds_page_shows_empty_state_for_samsung(client):
    body = client.get("/earbuds?brand=Samsung").get_data(as_text=True)
    assert "No earbuds match this filter." in body
    assert "AirPods" not in body


def test_phone_catalog_links_to_earbuds_and_keeps_phone_tabs(client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="/earbuds"' in body
    assert 'href="/?brand=Apple"' in body
    assert "AirPods" not in body


def test_earbud_detail_shows_airpods_pro_3_specs(app, client):
    resp = client.get(f"/earbuds/{_earbud_id(app, 'AirPods Pro 3')}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "AirPods Pro 3" in body
    assert "$249" in body
    assert "September 19, 2025" in body
    assert "Up to 8 hours (24 hours with case)" in body
    assert "Active Noise Cancellation" in body
    assert "IP57" in body
    assert "USB-C" in body
    assert "Current model" in body


def test_earbud_detail_airpods_max_has_no_case_total_or_rating(app, client):
    # Over-ear headphones: no charging case, and Apple lists no IP rating.
    body = client.get(f"/earbuds/{_earbud_id(app, 'AirPods Max 2')}").get_data(as_text=True)
    assert "Up to 20 hours" in body
    assert "with case" not in body
    assert "Not rated" in body


def test_earbud_detail_returns_404_for_unknown_id(client):
    resp = client.get("/earbuds/999999")
    assert resp.status_code == 404


def test_earbud_detail_rejects_non_numeric_id(client):
    assert client.get("/earbuds/airpods").status_code == 404
