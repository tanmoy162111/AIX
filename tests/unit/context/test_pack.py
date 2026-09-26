from __future__ import annotations

from aix.core.context.pack import Section, assemble_pack, estimate_tokens


def sec(name: str, chars: int, ch: str = "x") -> Section:
    return Section(name=name, text=ch * chars)


def test_estimate_is_ceil_chars_over_four() -> None:
    assert [estimate_tokens(t) for t in ("", "a", "abcd", "abcde")] == [0, 1, 1, 2]


def test_everything_fits_in_priority_order() -> None:
    pack = assemble_pack([sec("goal", 40), sec("handoffs", 40)], budget_tokens=1000)
    assert [s.name for s in pack.sections] == ["goal", "handoffs"]
    assert pack.dropped == [] and pack.truncated == [] and pack.tokens == 25
    assert pack.text.startswith("GOAL\n")


def test_truncates_from_the_bottom_and_drops_lower_priority() -> None:
    pack = assemble_pack(
        [sec("goal", 400), sec("handoffs", 400), sec("failures", 400), sec("facts", 400)],
        budget_tokens=150,
    )
    assert [s.name for s in pack.sections] == ["goal", "handoffs"]
    assert pack.truncated == ["handoffs"] and pack.dropped == ["failures", "facts"]
    assert pack.tokens <= 250


def test_goal_is_never_dropped_even_when_alone_over_budget() -> None:
    pack = assemble_pack([sec("goal", 4000), sec("facts", 40)], budget_tokens=100)
    assert [s.name for s in pack.sections] == ["goal"] and pack.truncated == ["goal"]
    assert pack.dropped == ["facts"] and pack.tokens <= 100


def test_cut_happens_at_a_line_boundary() -> None:
    text = "\n".join(f"line {i:03d}" for i in range(100))
    pack = assemble_pack([Section(name="goal", text=text)], budget_tokens=50)
    body = pack.sections[0].text
    assert body.endswith("[truncated]")
    assert all(ln.startswith("line ") or ln == "[truncated]" for ln in body.splitlines())


def test_empty_sections_are_skipped_and_secrets_redacted() -> None:
    pack = assemble_pack(
        [Section(name="goal", text='api_key = "abcdefghijklmnop1234567890"'), Section("facts", "")],
        budget_tokens=1000,
    )
    assert [s.name for s in pack.sections] == ["goal"]
    assert "abcdefghijklmnop1234567890" not in pack.text


def test_deterministic() -> None:
    a = assemble_pack([sec("goal", 500), sec("h", 500)], budget_tokens=200)
    assert a == assemble_pack([sec("goal", 500), sec("h", 500)], budget_tokens=200)
