from testlens.gateway import FakeGateway


def test_fake_gateway_returns_canned_response():
    gw = FakeGateway(response="boom")
    assert gw.generate("anything") == "boom"


def test_fake_gateway_records_calls():
    gw = FakeGateway()
    gw.generate("prompt A")
    gw.generate("prompt B")
    assert gw.calls == ["prompt A", "prompt B"]


def test_fake_gateway_plays_scripted_responses_in_order():
    # A scripted fake: reply differently on each call, like a real LLM
    # that misbehaves first and complies after a correction.
    gw = FakeGateway(responses=["garbage", "good JSON"])

    assert gw.generate("first") == "garbage"
    assert gw.generate("second") == "good JSON"


def test_fake_gateway_repeats_last_scripted_response_when_exhausted():
    # If the script runs out, keep returning the final reply instead of
    # crashing -- tests shouldn't fail because a loop called once more.
    gw = FakeGateway(responses=["only reply"])

    gw.generate("first")
    assert gw.generate("second") == "only reply"
