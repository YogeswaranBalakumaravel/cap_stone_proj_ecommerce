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
