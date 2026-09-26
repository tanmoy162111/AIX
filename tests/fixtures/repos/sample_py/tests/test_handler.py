from app.handler import handle_request


def test_health() -> None:
    r = handle_request("GET", "/health")
    assert r.status == 200 and r.body == {"status": "ok"}


def test_unknown_route_is_404() -> None:
    assert handle_request("GET", "/nope").status == 404
