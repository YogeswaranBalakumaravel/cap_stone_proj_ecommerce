import pytest

from app import create_app
from config import TestingConfig


@pytest.fixture
def app():
    app = create_app(config_object=TestingConfig)
    yield app


@pytest.fixture
def client(app):
    return app.test_client()
