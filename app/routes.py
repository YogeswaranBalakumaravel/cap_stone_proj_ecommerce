"""Blueprint: catalog index, phone detail, two-phone comparison, JSON API, health check."""
from flask import Blueprint, abort, jsonify, render_template, request

from .extensions import db
from .models import Phone

main_bp = Blueprint("main", __name__)

VALID_BRANDS = {"Apple", "Samsung"}
VALID_SORTS = {"price", "release_date"}

# Rows of the comparison table: (spec, label, which value is better).
# "lower" / "higher" mark the better phone; None means the row differs as text only.
COMPARE_SPECS = [
    ("brand", "Brand", None),
    ("model_name", "Model", None),
    ("tier", "Tier", None),
    ("price_usd", "Starting price", "lower"),
    ("release_date", "Release date", "higher"),
    ("screen_size_in", "Screen size", "higher"),
    ("chip", "Chip", None),
    ("ram_gb", "RAM", "higher"),
    ("storage_options_gb", "Storage options", "higher"),
    ("camera_summary", "Camera", None),
    ("is_current", "Status", None),
]


def _query_phones(brand, sort):
    query = Phone.query
    if brand in VALID_BRANDS:
        query = query.filter_by(brand=brand)

    if sort == "price":
        query = query.order_by(Phone.price_usd.asc())
    elif sort == "release_date":
        query = query.order_by(Phone.release_date.desc())
    else:
        query = query.order_by(Phone.brand.asc(), Phone.price_usd.desc())

    return query.all()


class CompareError(Exception):
    """A comparison request that can't be served: `status` is 400 or 404."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_compare_ids(raw):
    """Turn "?ids=a,b" into [a, b], or raise CompareError(400, reason)."""
    if raw is None or not raw.strip():
        raise CompareError(400, "Choose two phones to compare, for example ?ids=1,2.")
    parts = [part.strip() for part in raw.split(",")]
    if not all(part.isascii() and part.isdigit() for part in parts):
        raise CompareError(400, "Phone IDs must be whole numbers.")
    ids = [int(part) for part in parts]
    if len(ids) < 2:
        raise CompareError(400, "Choose one more phone to compare.")
    if len(ids) > 2:
        raise CompareError(400, "You can compare exactly two phones.")
    if ids[0] == ids[1]:
        raise CompareError(400, "Choose two different phones.")
    return ids


def _spec_value(phone, spec):
    """The value a spec is compared on: storage by its largest tier."""
    if spec == "storage_options_gb":
        return max((int(s) for s in phone.storage_options_list), default=0)
    return getattr(phone, spec)


def _display_value(phone, spec):
    if spec == "price_usd":
        return f"${phone.price_usd:,.0f}"
    if spec == "release_date":
        return phone.release_date.strftime("%B %d, %Y") if phone.release_date else "—"
    if spec == "screen_size_in":
        return f"{phone.screen_size_in}″"
    if spec == "ram_gb":
        return f"{phone.ram_gb} GB"
    if spec == "storage_options_gb":
        return ", ".join(f"{s} GB" for s in phone.storage_options_list)
    if spec == "is_current":
        return "Current flagship" if phone.is_current else "Not currently sold"
    return str(getattr(phone, spec))


def _compare_phones(raw_ids):
    """Shared comparison logic for /compare and /api/compare.

    Returns (first, second, rows). Each row says whether the two phones differ and,
    for specs with an objective winner, which one is better ("first", "second" or None).
    """
    ids = _parse_compare_ids(raw_ids)
    phones = [db.session.get(Phone, phone_id) for phone_id in ids]
    for phone_id, phone in zip(ids, phones, strict=True):
        if phone is None:
            raise CompareError(404, f"No phone with ID {phone_id}.")
    first, second = phones

    rows = []
    for spec, label, better_when in COMPARE_SPECS:
        if spec == "storage_options_gb":
            differs = first.storage_options_list != second.storage_options_list
        else:
            differs = getattr(first, spec) != getattr(second, spec)
        better = None
        a, b = _spec_value(first, spec), _spec_value(second, spec)
        if better_when and a != b:
            first_wins = a < b if better_when == "lower" else a > b
            better = "first" if first_wins else "second"
        rows.append(
            {
                "spec": spec,
                "label": label,
                "values": [_display_value(first, spec), _display_value(second, spec)],
                "differs": differs,
                "better": better,
            }
        )
    return first, second, rows


@main_bp.route("/")
def index():
    brand = request.args.get("brand")
    sort = request.args.get("sort")
    phones = _query_phones(brand, sort)
    preselect = request.args.get("compare", "")
    return render_template(
        "index.html",
        phones=phones,
        current_brand=brand if brand in VALID_BRANDS else "All",
        current_sort=sort if sort in VALID_SORTS else "",
        preselected_id=int(preselect) if preselect.isascii() and preselect.isdigit() else None,
    )


@main_bp.route("/phone/<int:phone_id>")
def detail(phone_id):
    phone = db.session.get(Phone, phone_id)
    if phone is None:
        abort(404)
    return render_template("detail.html", phone=phone)


@main_bp.route("/compare")
def compare():
    try:
        first, second, rows = _compare_phones(request.args.get("ids"))
    except CompareError as err:
        if err.status == 404:
            abort(404)
        return render_template("compare.html", error=err.message), err.status
    return render_template("compare.html", phones=[first, second], rows=rows)


@main_bp.route("/api/compare")
def api_compare():
    try:
        first, second, rows = _compare_phones(request.args.get("ids"))
    except CompareError as err:
        return jsonify(error=err.message), err.status
    by_position = {"first": first.id, "second": second.id}
    return jsonify(
        phones=[first.to_dict(), second.to_dict()],
        differences=[
            {
                "spec": row["spec"],
                "label": row["label"],
                "better": row["better"],
                "better_id": by_position.get(row["better"]),
            }
            for row in rows
            if row["differs"]
        ],
    )


@main_bp.route("/api/phones")
def api_phones():
    brand = request.args.get("brand")
    sort = request.args.get("sort")
    phones = _query_phones(brand, sort)
    return jsonify([p.to_dict() for p in phones])


@main_bp.route("/healthz")
def healthz():
    return jsonify(status="ok"), 200
