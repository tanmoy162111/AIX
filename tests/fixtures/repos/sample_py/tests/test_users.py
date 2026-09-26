import pytest

from app.users import UserStore


def test_add_and_get() -> None:
    store = UserStore()
    store.add("ann", "pw")
    assert store.get("ann") is not None
    assert store.get("bob") is None


def test_duplicate_user_rejected() -> None:
    store = UserStore()
    store.add("ann", "pw")
    with pytest.raises(ValueError):
        store.add("ann", "other")


def test_verify() -> None:
    store = UserStore()
    store.add("ann", "pw")
    assert store.verify("ann", "pw")
    assert not store.verify("ann", "nope")
    assert not store.verify("ghost", "pw")
