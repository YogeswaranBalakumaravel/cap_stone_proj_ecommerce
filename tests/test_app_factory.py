import os

from flask import Flask

from app import create_app
from app.models import Phone
from config import TestingConfig


def test_create_app_returns_flask_instance():
    app = create_app(config_object=TestingConfig)
    assert isinstance(app, Flask)


def test_create_app_applies_given_config():
    app = create_app(config_object=TestingConfig)
    assert app.config["TESTING"] is True
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"


def test_create_app_defaults_to_config_module(monkeypatch):
    # No config_object passed -> falls back to `from config import Config`.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    app = create_app()
    assert app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False


def test_create_app_creates_instance_path():
    app = create_app(config_object=TestingConfig)
    assert os.path.isdir(app.instance_path)


def test_create_app_uses_instance_relative_config():
    # create_app passes instance_relative_config=True to Flask, which makes
    # app.config resolve relative paths (e.g. from_pyfile calls) against the
    # instance folder rather than the app's package root.
    app = create_app(config_object=TestingConfig)
    assert app.config.root_path == app.instance_path


def test_create_app_registers_main_blueprint():
    app = create_app(config_object=TestingConfig)
    assert "main" in app.blueprints


def test_create_app_creates_tables_and_seeds_data():
    app = create_app(config_object=TestingConfig)
    with app.app_context():
        assert Phone.query.count() > 0


def test_create_app_registers_404_handler():
    app = create_app(config_object=TestingConfig)
    client = app.test_client()
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert b"couldn't find that phone" in resp.data
