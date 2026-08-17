"""Application factory."""
import os

from flask import Flask, render_template

from .extensions import db


def create_app(config_object=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    if config_object is None:
        from config import Config

        config_object = Config
    app.config.from_object(config_object)

    db.init_app(app)

    from .routes import main_bp

    app.register_blueprint(main_bp)

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("404.html"), 404

    with app.app_context():
        from .seed_data import seed_if_empty

        db.create_all()
        seed_if_empty()

    return app
