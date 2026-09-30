from html.parser import HTMLParser

from app.models import Phone


def _ids(app, *names):
    with app.app_context():
        return [Phone.query.filter_by(model_name=n).one().id for n in names]


class _CompareTable(HTMLParser):
    """Collect each comparison row: spec -> (differs, better position or None)."""

    def __init__(self):
        super().__init__()
        self.rows, self.headers = {}, []
        self._spec, self._cell = None, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "th" and "data-phone-id" in attrs:
            self.headers.append(int(attrs["data-phone-id"]))
        if tag == "tr" and "data-spec" in attrs:
            self._spec, self._cell = attrs["data-spec"], 0
            self.rows[self._spec] = [attrs["class"] == "differs", None]
        if tag == "td" and self._spec:
            self._cell += 1
            if "better" in (attrs.get("class") or "").split():
                self.rows[self._spec][1] = "first" if self._cell == 1 else "second"


def _table(client, query):
    resp = client.get(f"/compare?{query}")
    parser = _CompareTable()
    parser.feed(resp.get_data(as_text=True))
    return resp, parser


def test_compare_page_lists_every_spec_for_both_phones(app, client):
    a, b = _ids(app, "iPhone 18 Pro Max", "Galaxy S26 Ultra")
    resp, table = _table(client, f"ids={a},{b}")
    assert resp.status_code == 200
    assert table.headers == [a, b]
    assert list(table.rows) == [
        "brand",
        "model_name",
        "tier",
        "price_usd",
        "release_date",
        "screen_size_in",
        "chip",
        "ram_gb",
        "storage_options_gb",
        "camera_summary",
        "is_current",
    ]
    body = resp.get_data(as_text=True)
    assert "$1,299" in body
    assert "256 GB, 512 GB, 1024 GB, 2048 GB" in body


def test_compare_apple_vs_samsung_highlights_and_marks_the_right_rows(app, client):
    # Same price, screen, RAM and status; the iPhone is newer and goes to 2TB.
    a, b = _ids(app, "iPhone 18 Pro Max", "Galaxy S26 Ultra")
    _, table = _table(client, f"ids={a},{b}")
    assert table.rows == {
        "brand": [True, None],
        "model_name": [True, None],
        "tier": [True, None],
        "price_usd": [False, None],
        "release_date": [True, "first"],
        "screen_size_in": [False, None],
        "chip": [True, None],
        "ram_gb": [False, None],
        "storage_options_gb": [True, "first"],
        "camera_summary": [True, None],
        "is_current": [False, None],
    }


def test_compare_two_iphones_marks_price_screen_ram_and_storage(app, client):
    a, b = _ids(app, "iPhone 17", "iPhone 18 Pro Max")
    _, table = _table(client, f"ids={a},{b}")
    better = {spec: row[1] for spec, row in table.rows.items() if row[1]}
    assert better == {
        "price_usd": "first",
        "release_date": "second",
        "screen_size_in": "second",
        "ram_gb": "second",
        "storage_options_gb": "second",
    }
    assert table.rows["brand"] == [False, None]
    assert table.rows["chip"] == [True, None]


def test_compare_keeps_the_order_of_the_ids(app, client):
    a, b = _ids(app, "iPhone 17", "iPhone 18 Pro Max")
    _, table = _table(client, f"ids={b},{a}")
    assert table.headers == [b, a]
    assert table.rows["price_usd"] == [True, "second"]
    assert table.rows["ram_gb"] == [True, "first"]


def test_compare_same_max_storage_differs_without_a_winner(app, client):
    # Both top out at 2TB with the same tiers, and share the chip and RAM.
    a, b = _ids(app, "iPhone 18 Pro", "iPhone 18 Pro Max")
    _, table = _table(client, f"ids={a},{b}")
    assert table.rows["storage_options_gb"] == [False, None]
    assert table.rows["chip"] == [False, None]
    assert table.rows["screen_size_in"] == [True, "second"]
    assert table.rows["price_usd"] == [True, "first"]
    assert table.rows["release_date"] == [False, None]


def test_compare_storage_uses_the_largest_tier(app, client):
    # Different tier lists: 256/512/1024 vs 256/512/1024/2048.
    a, b = _ids(app, "iPhone Air", "iPhone 18 Pro")
    _, table = _table(client, f"ids={a},{b}")
    assert table.rows["storage_options_gb"] == [True, "second"]


def test_compare_accepts_whitespace_around_ids(app, client):
    a, b = _ids(app, "iPhone 17", "Galaxy S26")
    resp, table = _table(client, f"ids=%20{a}%20,%20{b}%20")
    assert resp.status_code == 200
    assert table.headers == [a, b]


def test_api_compare_matches_the_page(app, client):
    a, b = _ids(app, "iPhone 18 Pro Max", "Galaxy S26 Ultra")
    data = client.get(f"/api/compare?ids={a},{b}").get_json()
    assert [p["id"] for p in data["phones"]] == [a, b]
    assert data["phones"][0]["model_name"] == "iPhone 18 Pro Max"
    assert data["phones"][0]["storage_options_gb"] == ["256", "512", "1024", "2048"]

    _, table = _table(client, f"ids={a},{b}")
    differing = {spec: row[1] for spec, row in table.rows.items() if row[0]}
    assert {d["spec"]: d["better"] for d in data["differences"]} == differing
    assert {d["spec"]: d["better_id"] for d in data["differences"] if d["better"]} == {
        "release_date": a,
        "storage_options_gb": a,
    }


def test_compare_rejects_bad_ids_with_400(app, client):
    a, b = _ids(app, "iPhone 17", "Galaxy S26")
    cases = {
        "": "Choose two phones to compare",
        "ids=": "Choose two phones to compare",
        f"ids={a}": "Choose one more phone",
        f"ids={a},{b},{a}": "exactly two phones",
        f"ids={a},{a}": "two different phones",
        f"ids={a},abc": "whole numbers",
        f"ids={a},-{b}": "whole numbers",
        f"ids={a},1.5": "whole numbers",
        f"ids={a},": "whole numbers",
    }
    for query, message in cases.items():
        page = client.get(f"/compare?{query}")
        assert page.status_code == 400, query
        assert message in page.get_data(as_text=True), query

        api = client.get(f"/api/compare?{query}")
        assert api.status_code == 400, query
        assert message in api.get_json()["error"], query


def test_compare_unknown_id_returns_404(app, client):
    (a,) = _ids(app, "iPhone 17")
    page = client.get(f"/compare?ids={a},999999")
    assert page.status_code == 404
    assert b"couldn't find that phone" in page.data

    api = client.get(f"/api/compare?ids=999999,{a}")
    assert api.status_code == 404
    assert api.get_json() == {"error": "No phone with ID 999999."}


def test_catalog_has_a_compare_box_per_phone_and_hides_the_button(client):
    body = client.get("/").get_data(as_text=True)
    with client.application.app_context():
        count = Phone.query.count()
    assert body.count('class="compare-box"') == count
    assert '<div class="compare-bar" id="compare-bar" hidden>' in body
    assert 'id="compare-button" href="/compare" hidden' in body


def test_detail_links_to_the_catalog_with_the_phone_ticked(app, client):
    (a,) = _ids(app, "iPhone 18 Pro")
    detail = client.get(f"/phone/{a}").get_data(as_text=True)
    assert f'href="/?compare={a}"' in detail

    catalog = client.get(f"/?compare={a}").get_data(as_text=True)
    assert f'value="{a}"\n                 checked' in catalog
    assert catalog.count("checked>") == 1


def test_catalog_ignores_a_bad_compare_value(client):
    body = client.get("/?compare=abc").get_data(as_text=True)
    assert "checked>" not in body
