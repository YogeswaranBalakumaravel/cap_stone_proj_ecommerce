"""Blueprint: catalog index, phone detail, JSON API, health check."""
from flask import Blueprint, abort, jsonify, render_template, request

from .extensions import db
from .models import Phone

main_bp = Blueprint("main", __name__)

VALID_BRANDS = {"Apple", "Samsung"}
VALID_SORTS = {"price", "release_date"}


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


@main_bp.route("/")
def index():
    brand = request.args.get("brand")
    sort = request.args.get("sort")
    phones = _query_phones(brand, sort)
    return render_template(
        "index.html",
        phones=phones,
        current_brand=brand if brand in VALID_BRANDS else "All",
        current_sort=sort if sort in VALID_SORTS else "",
    )


@main_bp.route("/phone/<int:phone_id>")
def detail(phone_id):
    phone = db.session.get(Phone, phone_id)
    if phone is None:
        abort(404)
    return render_template("detail.html", phone=phone)


@main_bp.route("/api/phones")
def api_phones():
    brand = request.args.get("brand")
    sort = request.args.get("sort")
    phones = _query_phones(brand, sort)
    return jsonify([p.to_dict() for p in phones])


@main_bp.route("/healthz")
def healthz():
    return jsonify(status="ok"), 200
