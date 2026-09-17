"""Behaviour of the mock bank app, including every injectable fault."""

import pytest

from target_app import create_app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("TARGET_APP_USER", "teller")
    monkeypatch.setenv("TARGET_APP_PASSWORD", "teller-pass")
    monkeypatch.delenv("FAULT", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post("/login", data={"u": "teller", "p": "teller-pass"})
    return c


def text(resp) -> str:
    return resp.get_data(as_text=True)


def test_requires_login(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    resp = c.get("/members/search")
    assert resp.status_code == 401
    assert "session has expired" in text(resp)


def test_bad_password(app):
    resp = app.test_client().post("/login", data={"u": "teller", "p": "wrong"})
    assert "Invalid user ID or password" in text(resp)


def test_search_found_goes_to_detail(client):
    resp = client.post("/members/find", data={"mid": "10023"}, follow_redirects=True)
    body = text(resp)
    assert "Member Detail" in body
    assert "Share Savings" in body


def test_search_not_found_is_shown_on_page(client):
    resp = client.post("/members/find", data={"mid": "99999"})
    assert resp.status_code == 200
    assert "No member found for ID 99999" in text(resp)


def test_search_invalid_format(client):
    assert "must be 5 digits" in text(client.post("/members/find", data={"mid": "12"}))


def test_frozen_member_not_eligible(client):
    assert "not eligible for new sub-accounts" in text(client.get("/accounts/new?mid=10013"))


def test_deposit_below_minimum(client):
    resp = client.post("/accounts/review", data={"mid": "10023", "f1": "Savings", "f2": "Rainy Day", "f3": "5"})
    assert "Initial deposit must be between" in text(resp)


def test_open_sub_account_happy_path(client):
    form = {"mid": "10023", "f1": "Money Market", "f2": "Rainy Day", "f3": "250.00"}
    assert "Review Sub-Account" in text(client.post("/accounts/review", data=form))
    assert "Sub-Account Opened" in text(client.post("/accounts/confirm", data=form))
    assert "Rainy Day" in text(client.get("/members/10023"))


@pytest.mark.parametrize("fault, status, marker", [
    ("app_error", 500, "Application Error"),
    ("permission_denied", 403, "Insufficient privileges"),
])
def test_blocking_faults(app, client, fault, status, marker):
    app.config["FAULT"] = fault
    resp = client.get("/members/search")
    assert resp.status_code == status
    assert marker in text(resp)


def test_session_timeout_is_one_shot(app, client):
    app.config["FAULT"] = "session_timeout"
    assert client.get("/members/search").status_code == 401
    client.post("/login", data={"u": "teller", "p": "teller-pass"})
    assert client.get("/members/search").status_code == 200


def test_interstitial_shown_once_per_session(app, client):
    app.config["FAULT"] = "interstitial"
    assert "System Notice" in text(client.get("/members/search"))
    assert "System Notice" not in text(client.get("/members/search"))


def test_admin_fault_endpoint(app):
    c = app.test_client()
    assert c.post("/admin/fault", data={"name": "slow"}).get_json() == {"fault": "slow"}
    assert c.post("/admin/fault", data={"name": "bogus"}).status_code == 400
    assert c.post("/admin/fault", data={"name": ""}).get_json() == {"fault": None}
