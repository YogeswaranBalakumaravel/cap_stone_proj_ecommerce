from app.models import Phone


def test_index_returns_200_and_both_brands(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Apple" in body
    assert "Samsung" in body


def test_index_filters_by_brand(client):
    resp = client.get("/?brand=Apple")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "iPhone" in body
    assert "Galaxy" not in body


def test_index_sort_by_price(client):
    resp = client.get("/?sort=price")
    assert resp.status_code == 200


def test_index_ignores_invalid_brand(client):
    resp = client.get("/?brand=Nokia")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Falls back to showing all brands rather than filtering/erroring.
    assert "iPhone" in body
    assert "Galaxy" in body


def test_index_ignores_invalid_sort(client):
    resp = client.get("/?sort=popularity")
    assert resp.status_code == 200


def test_api_phones_filters_by_brand(client):
    resp = client.get("/api/phones?brand=Samsung")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) > 0
    assert all(p["brand"] == "Samsung" for p in data)


def test_api_phones_sort_by_price_is_ascending(client):
    resp = client.get("/api/phones?sort=price")
    data = resp.get_json()
    prices = [p["price_usd"] for p in data]
    assert prices == sorted(prices)


def test_api_phones_default_sort_is_brand_then_price_descending(client):
    # No ?sort= given -> falls into _query_phones' else branch: brand
    # ascending, then price descending within each brand.
    resp = client.get("/api/phones")
    data = resp.get_json()
    keys = [(p["brand"], -p["price_usd"]) for p in data]
    assert keys == sorted(keys)


def test_api_phones_sort_by_release_date_is_descending(client):
    resp = client.get("/api/phones?sort=release_date")
    data = resp.get_json()
    dates = [p["release_date"] for p in data]
    assert dates == sorted(dates, reverse=True)


def test_api_phones_combines_brand_filter_and_sort(client):
    resp = client.get("/api/phones?brand=Apple&sort=price")
    data = resp.get_json()
    assert all(p["brand"] == "Apple" for p in data)
    prices = [p["price_usd"] for p in data]
    assert prices == sorted(prices)


def test_unknown_route_returns_custom_404_page(client):
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert b"couldn't find that phone" in resp.data


def test_detail_returns_200_for_valid_id(app, client):
    with app.app_context():
        phone_id = Phone.query.first().id

    resp = client.get(f"/phone/{phone_id}")
    assert resp.status_code == 200


def test_detail_returns_404_for_invalid_id(client):
    resp = client.get("/phone/999999")
    assert resp.status_code == 404


def test_api_phones_returns_json_list(client):
    resp = client.get("/api/phones")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) > 0
    assert "brand" in data[0]


def test_healthz_returns_200(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_api_phones_apple_lineup_matches_apple_store(client):
    # Sep 2026: iPhone 18 Pro and Pro Max added, iPhone 17 Pro and Pro Max
    # discontinued, iPhone 17 and Air up $100. Default sort: price descending.
    data = client.get("/api/phones?brand=Apple").get_json()
    assert [(p["model_name"], p["price_usd"]) for p in data] == [
        ("iPhone 18 Pro Max", 1299.0),
        ("iPhone 18 Pro", 1199.0),
        ("iPhone Air", 1099.0),
        ("iPhone 17", 899.0),
    ]


def test_api_phones_iphone_18_pro_models_have_launch_specs(client):
    phones = {p["model_name"]: p for p in client.get("/api/phones?brand=Apple").get_json()}
    for name, tier, screen_size in (
        ("iPhone 18 Pro", "Pro", 6.3),
        ("iPhone 18 Pro Max", "Pro Max", 6.9),
    ):
        phone = phones[name]
        assert phone["tier"] == tier
        assert phone["screen_size_in"] == screen_size
        assert phone["chip"] == "A20 Pro"
        assert phone["ram_gb"] == 12
        assert phone["release_date"] == "2026-09-18"
        assert phone["storage_options_gb"] == ["256", "512", "1024", "2048"]
        assert phone["is_current"] is True


def test_api_phones_iphone_17_starts_at_256gb(client):
    phones = {p["model_name"]: p for p in client.get("/api/phones?brand=Apple").get_json()}
    assert phones["iPhone 17"]["storage_options_gb"] == ["256", "512"]


def test_api_phones_newest_first_starts_with_iphone_18_pro_models(client):
    data = client.get("/api/phones?sort=release_date").get_json()
    assert {p["model_name"] for p in data[:2]} == {"iPhone 18 Pro", "iPhone 18 Pro Max"}
    assert max(p["release_date"] for p in data[2:]) < "2026-09-18"


def test_index_no_longer_lists_discontinued_iphone_17_pro_models(client):
    body = client.get("/?brand=Apple").get_data(as_text=True)
    assert "iPhone 18 Pro Max" in body
    assert "iPhone 17 Pro" not in body


def test_detail_shows_iphone_18_pro_max_specs(app, client):
    with app.app_context():
        phone_id = Phone.query.filter_by(model_name="iPhone 18 Pro Max").one().id

    body = client.get(f"/phone/{phone_id}").get_data(as_text=True)
    assert "iPhone 18 Pro Max" in body
    assert "$1,299" in body
    assert "A20 Pro" in body
    assert "256 GB, 512 GB, 1024 GB, 2048 GB" in body
    assert "Current flagship" in body
