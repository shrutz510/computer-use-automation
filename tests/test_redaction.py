from cua.redaction import Redactor


def test_patterns():
    r = Redactor()
    assert r.text("SSN 900-12-3456") == "SSN ***-**-3456"
    assert r.text("acct 7100231234") == "acct ****1234"
    assert r.text("born 1971-04-09") == "born [DATE]"
    assert r.text("member 10023") == "member 10023"  # short ids are not PII
    assert r.text("$1,234.56") == "$1,234.56"         # balances are the outputs we need


def test_learned_values_are_scrubbed_everywhere():
    r = Redactor()
    r.learn(["Alex Sample", "x"])  # too-short values are ignored
    assert r.scrub({"a": ["Hello Alex Sample"], "n": 3}) == {"a": ["Hello [REDACTED]"], "n": 3}
    assert r.text("x marks") == "x marks"
