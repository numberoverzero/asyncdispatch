import pytest


def test_start_idempotent(loop, dispatch):
    loop.run_until_complete(dispatch.start())
    assert dispatch.running

    loop.run_until_complete(dispatch.start())
    assert dispatch.running

    # Prevent complaints about destroying a pending task
    loop.run_until_complete(dispatch.stop())


def test_stop_idempotent(loop, dispatch):
    loop.run_until_complete(dispatch.start())
    assert dispatch.running

    loop.run_until_complete(dispatch.stop())
    assert not dispatch.running

    loop.run_until_complete(dispatch.stop())
    assert not dispatch.running


def test_clean_stop(loop, dispatch):
    """ Stop ensures the main dispatch loop shuts down gracefully """
    loop.run_until_complete(dispatch.start())
    loop.run_until_complete(dispatch.stop())


def test_unknown_event(dispatch):
    """
    An exception should be thrown when trying to register a
    handler for an unknown event.
    """
    with pytest.raises(ValueError):
        dispatch.on("unknown")


def test_register(dispatch):
    event = "my-event"
    params = ["x", "y", "z"]
    dispatch.register(event, params)
    assert "my-event" in dispatch._handlers


def test_register_twice(dispatch):
    event = "my-event"
    params = ["x", "y", "z"]
    dispatch.register(event, params)

    with pytest.raises(ValueError):
        dispatch.register(event, params)


def test_register_running(dispatch, loop):
    event = "my-event"
    params = ["x", "y", "z"]
    loop.run_until_complete(dispatch.start())

    with pytest.raises(RuntimeError):
        dispatch.register(event, params)

    # Prevent complaints about destroying a pending task
    loop.run_until_complete(dispatch.stop())


def test_unregister_unknown(dispatch):
    assert "unknown-event" not in dispatch._handlers
    dispatch.unregister("unknown-event")


def test_unregister_running(dispatch, loop):
    event = "my-event"
    params = ["x", "y", "z"]
    dispatch.register(event, params)

    loop.run_until_complete(dispatch.start())
    with pytest.raises(RuntimeError):
        dispatch.unregister(event)

    # Prevent complaints about destroying a pending task
    loop.run_until_complete(dispatch.stop())


def test_single_handler(dispatch, loop):
    event = "my-event"
    expected = {"x": 4, "y": 5, "z": 6}
    dispatch.register(event, expected.keys())

    called = False

    @dispatch.on(event)
    async def handle(kwargs):
        nonlocal called
        assert kwargs == expected
        called = True

    for task in [
        dispatch.start(),
        dispatch.trigger(event, expected),
        dispatch.stop()
    ]:
        loop.run_until_complete(task)
    assert called


def test_clear(dispatch, loop):
    event = "my-event"
    kwargs = {"foo": 4}
    dispatch.register(event, ["foo"])

    @dispatch.on(event)
    async def handle(kwargs):
        assert kwargs["foo"] == 4

    loop.run_until_complete(dispatch.trigger(event, kwargs))
    assert dispatch.events
    dispatch.clear()
    assert not dispatch.events


def test_trigger_unknown(dispatch, loop):
    for task in [
        dispatch.start(),
        dispatch.trigger("unknown-event", {"not": "used"}),
        dispatch.stop()
    ]:
        loop.run_until_complete(task)
