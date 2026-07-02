from testlens.gateway import FakeGateway


def test_fake_gateway_returns_canned_response():
    gw = FakeGateway(response="boom")
    assert gw.generate("anything") == "boom"


def test_fake_gateway_records_calls():
    gw = FakeGateway()
    gw.generate("prompt A")
    gw.generate("prompt B")
    assert gw.calls == ["prompt A", "prompt B"]
