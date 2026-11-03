import pytest

from tests import fake_llm
from tests.support import create_database, drop_database, kill_all


@pytest.fixture(scope="module")
def llm():
    server, url = fake_llm.start()
    yield url
    server.shutdown()


@pytest.fixture(scope="module")
def db():
    url = create_database()
    yield url
    kill_all()
    drop_database(url)
