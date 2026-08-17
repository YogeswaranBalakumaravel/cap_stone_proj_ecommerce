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
