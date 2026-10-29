"""The code review agent's eleventh check, performance and resilience
(.qa/code-review/checks/resilience.py): N+1 queries, unpaginated list endpoints, unthrottled login
and cart writes, outbound calls without a timeout, and payment/shipping calls without retries.

The check reads source text and never runs it, so each test hands it a snippet as a changed file.
"""

import importlib.util
import json
import sys
import textwrap
import types
from pathlib import Path

CR = Path(__file__).resolve().parents[1] / ".qa" / "code-review"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cr_common = _load("cr_common", CR / "cr_common.py")
# A bare package for the two check modules, so the other checks (and their imports) aren't loaded.
package = types.ModuleType("cr_checks")
package.__path__ = [str(CR / "checks")]
sys.modules["cr_checks"] = package
resilience = _load("cr_checks.resilience", CR / "checks" / "resilience.py")
errors = _load("cr_checks.errors", CR / "checks" / "errors.py")
attest = _load("cr_attest", CR / "attest.py")
CONFIG = json.loads((CR / "config.json").read_text(encoding="utf-8"))


class FakeCtx:
    """The parts of cr_common.Ctx the checks use, over in-memory files."""

    def __init__(self, files, added=None, tracked=None):
        self.cfg = CONFIG
        self.texts = {p: textwrap.dedent(t).lstrip("\n") for p, t in files.items()}
        self.files = [{"path": p, "status": "modified", "area": "backend"} for p in files]
        everything = {p: set(range(1, t.count("\n") + 2)) for p, t in self.texts.items()}
        self.added = {p: set(added[p]) if added and p in added else everything[p] for p in files}
        self.tracked = list(files) + list(tracked or {})
        self.texts.update(tracked or {})

    def changed(self, *areas):
        return [f for f in self.files if not areas or f["area"] in areas]

    def head(self, path):
        return self.texts.get(path)


def review(code, path="app/routes.py", **kw):
    return resilience.run(FakeCtx({path: code}, **kw), {})


def rules(found):
    return sorted((f["rule"], f["line"]) for f in found)


# ------------------------------------------------------------------ registration


def test_check_eleven_is_registered_as_a_standard_row():
    check = CONFIG["checks"]["performance_resilience"]
    assert list(CONFIG["checks"])[-1] == "performance_resilience"
    assert len(CONFIG["checks"]) == 11
    assert check["title"] == "Performance and resilience"
    assert check["row"] == "STANDARD"
    assert not check.get("human_attestation")


def test_collect_runs_the_check():
    init = (CR / "checks" / "__init__.py").read_text(encoding="utf-8")
    assert init.count("resilience,") == 2  # imported, and listed in ALL


def test_the_agent_may_report_on_the_check():
    schema = json.loads((CR / "schemas" / "review.schema.json").read_text(encoding="utf-8"))
    props = schema["properties"]
    assert "performance_resilience" in props["check_summaries"]["properties"]
    assert "performance_resilience" in props["findings"]["items"]["properties"]["check"]["enum"]
    prompt = (CR / "prompts" / "review.md").read_text(encoding="utf-8")
    assert "11. `performance_resilience`" in prompt


def test_reviewers_can_attest_the_check_by_alias():
    for word in ("performance", "perf", "resilience", "performance_resilience"):
        assert attest.parse(f"attest: {word}", CONFIG) == {"performance_resilience"}
    assert "performance_resilience" in attest.parse("attest: all", CONFIG)


def test_every_finding_is_a_signal_on_the_check():
    found = review(
        """
        import requests

        def sync(ids):
            for i in ids:
                Phone.query.get(i)
            requests.get("https://example.test")
        """
    )
    assert found
    assert {f["check"] for f in found} == {"performance_resilience"}
    assert not any(f["hard"] for f in found)


# ------------------------------------------------------------------ PF001 query count


def test_query_inside_a_loop_is_n_plus_one():
    found = review(
        """
        def names(ids):
            out = []
            for i in ids:
                out.append(db.session.get(Phone, i).name)
            return out
        """
    )
    assert rules(found) == [("PF001", 4)]
    assert found[0]["severity"] == "medium"


def test_query_in_a_comprehension_or_while_loop_is_n_plus_one():
    found = review(
        """
        def names(ids):
            return [Phone.query.filter_by(id=i).first() for i in ids]

        def drain(queue):
            while queue:
                Earbud.query.get_or_404(queue.pop())
        """
    )
    assert rules(found) == [("PF001", 2), ("PF001", 6)]


def test_one_query_before_the_loop_is_fine():
    found = review(
        """
        def names(ids):
            phones = Phone.query.filter(Phone.id.in_(ids)).all()
            return [p.name for p in phones]
        """
    )
    assert rules(found) == []


def test_loop_over_a_query_result_is_not_a_query_per_item():
    found = review(
        """
        def names():
            for p in Phone.query.all():
                print(p.name)
        """
    )
    assert rules(found) == []


def test_existing_n_plus_one_the_pr_doesnt_touch_is_not_reported():
    code = """
        def names(ids):
            for i in ids:
                Phone.query.get(i)
            return 1
        """
    assert rules(review(code, added={"app/routes.py": {4}})) == []
    assert rules(review(code, added={"app/routes.py": {3}})) == [("PF001", 3)]


# ------------------------------------------------------------------ PF002 pagination


CATALOG = """
    def _query_catalog(model):
        query = model.query
        return query.order_by(model.price_usd).all()

    def _query_phones():
        return _query_catalog(Phone)

    @main_bp.route("/api/phones")
    def api_phones():
        return jsonify([p.to_dict() for p in _query_phones()])
    """


def test_new_list_endpoint_through_helpers_isnt_paginated():
    found = review(CATALOG)
    assert rules(found) == [("PF002", 3)]  # the new .all(), found two helpers down
    assert "/api/phones" in found[0]["title"]


def test_new_route_over_an_existing_unpaginated_helper_is_reported_on_the_route():
    found = review(CATALOG, added={"app/routes.py": {8, 9, 10}})
    assert rules(found) == [("PF002", 8)]


def test_untouched_list_endpoint_is_not_reported():
    # Only the route's body changed: neither the route nor the query is new.
    assert rules(review(CATALOG, added={"app/routes.py": {10}})) == []


def test_limit_or_paginate_counts_as_pagination():
    for paged in ("query.limit(50).all()", "query.offset(o).limit(50).all()"):
        code = f"""
            @main_bp.route("/api/phones")
            def api_phones():
                query = Phone.query
                return jsonify([p.to_dict() for p in {paged}])
            """
        assert rules(review(code)) == []
    code = """
        @main_bp.get("/api/phones")
        def api_phones():
            page = Phone.query.paginate(page=1, per_page=20)
            return jsonify([p.to_dict() for p in page.items])
        """
    assert rules(review(code)) == []


def test_detail_and_write_routes_are_not_list_endpoints():
    code = """
        @main_bp.route("/phone/<int:phone_id>")
        def detail(phone_id):
            return str(Phone.query.all())

        @main_bp.route("/api/phones", methods=["POST"])
        def create():
            return str(Phone.query.all())
        """
    assert [r for r, _ in rules(review(code))] == []


def test_session_scalars_all_is_a_list_query():
    code = """
        @main_bp.get("/api/earbuds")
        def api_earbuds():
            return jsonify(db.session.scalars(select(Earbud)).all())
        """
    assert rules(review(code)) == [("PF002", 3)]


# ------------------------------------------------------------------ PF003 rate limiting


def test_login_post_without_a_rate_limit_is_high():
    found = review(
        """
        @main_bp.route("/login", methods=["GET", "POST"])
        def login():
            return check(request.form)
        """
    )
    assert rules(found) == [("PF003", 1)]
    assert found[0]["severity"] == "high"
    assert found[0]["explanation"] == (
        "Credential stuffing and brute force; nothing caps requests per client."
    )


def test_cart_write_without_a_rate_limit_is_medium():
    found = review(
        """
        @main_bp.post("/cart/add")
        def add_to_cart():
            return "ok"
        """
    )
    assert rules(found) == [("PF003", 1)]
    assert found[0]["severity"] == "medium"
    assert found[0]["explanation"] == (
        "Bots can flood the cart and hold stock; nothing caps requests per client."
    )


def test_route_name_counts_as_well_as_its_path():
    found = review(
        """
        @main_bp.route("/session", methods=["POST"])
        def sign_in():
            return "ok"
        """
    )
    assert rules(found) == [("PF003", 1)]


def test_rate_limited_login_is_fine():
    code = """
        @main_bp.route("/login", methods=["POST"])
        @limiter.limit("5 per minute")
        def login():
            return "ok"
        """
    assert rules(review(code)) == []


def test_app_wide_default_limits_cover_every_route():
    code = """
        @main_bp.route("/login", methods=["POST"])
        def login():
            return "ok"
        """
    limiter = 'limiter = Limiter(get_remote_address, default_limits=["100 per hour"])\n'
    assert rules(review(code, tracked={"app/extensions.py": limiter})) == []


def test_reading_login_or_cart_pages_needs_no_rate_limit():
    code = """
        @main_bp.route("/login")
        def login_page():
            return render_template("login.html")

        @main_bp.route("/phones", methods=["POST"])
        def add_phone():
            return "ok"
        """
    assert [r for r, _ in rules(review(code))] == []


# ------------------------------------------------------------------ PF004 timeouts


def test_outbound_call_without_a_timeout():
    found = review(
        """
        import requests
        from urllib.request import urlopen

        def fetch(url):
            urlopen(url)
            return requests.get(url, timeout=5)
        """
    )
    assert rules(found) == [("PF004", 5)]
    assert found[0]["title"] == "urlopen() without a timeout"


def test_timeouts_are_no_longer_reported_by_error_handling():
    code = """
        import requests

        def fetch(url):
            return requests.get(url)
        """
    assert rules(review(code)) == [("PF004", 4)]
    assert rules(errors.run(FakeCtx({"app/x.py": code}), {})) == []


# ------------------------------------------------------------------ PF005 retries


def test_payment_sdk_call_without_retry():
    found = review(
        """
        import stripe

        def charge(order):
            return stripe.PaymentIntent.create(amount=order.total)
        """
    )
    assert rules(found) == [("PF005", 4)]


def test_shipping_http_call_without_retry():
    found = review(
        """
        import requests

        def book_shipment(parcel):
            return requests.post(SHIPPING_URL, json=parcel, timeout=10)
        """
    )
    assert rules(found) == [("PF005", 4)]


def test_retry_or_circuit_breaker_in_the_module_is_fine():
    for guard in (
        "from tenacity import retry",
        "breaker = pybreaker.CircuitBreaker(fail_max=5)",
        "adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1))",
    ):
        code = f"""
            import requests
            {guard}

            def book_shipment(parcel):
                return requests.post(SHIPPING_URL, json=parcel, timeout=10)
            """
        assert rules(review(code)) == [], guard


def test_ordinary_outbound_calls_need_no_retry():
    # "payload" and "relationship" contain "pay" and "ship" but aren't about either.
    found = review(
        """
        import requests

        def notify(payload, relationship):
            return requests.post(WEBHOOK_URL, json=payload, timeout=5)
        """
    )
    assert rules(found) == []


def test_non_python_and_unparsable_files_are_skipped():
    assert review("SELECT * FROM phones;", path="app/schema.sql") == []
    assert review("def broken(:\n    requests.get(x)\n") == []
