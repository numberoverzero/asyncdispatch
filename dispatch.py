import asyncio
import inspect
missing = object()


def noop(*a, **kw):
    pass


class Dispatch(object):
    ''' Dispatch unpacked **kwargs to callbacks when events occur '''
    def __init__(self, loop):
        self._handlers = {}
        self._loop = loop
        self._queue = asyncio.Queue(loop=self._loop)
        self.running = False
        self._start_shutdown = asyncio.Event(loop=self._loop)
        self._shutdown_complete = asyncio.Event(loop=self._loop)
        self._resume_processing = asyncio.Event(loop=self._loop)

    def on(self, event):
        '''
        Returns a wrapper for the given event.

        Usage:

            @dispatch.on("my_event")
            def handle_my_event(foo, bar, baz):
                ...

        '''
        handler = self._handlers.get(event, None)
        if not handler:
            raise ValueError("Unknown event '{}'".format(event))
        return handler.register

    def trigger(self, event, params):
        ''' Non-blocking enqueue of an event '''
        self._queue.put_nowait((event, params))
        self._resume_processing.set()

    def register(self, event, params):
        '''
        Register a new event with available params.
        Raises ValueError when the event has already been registered.

        Usage:

            dispatch.register("my_event", ["foo", "bar", "baz"])

        '''
        handler = self._handlers.get(event, missing)
        if handler is not missing:
            raise ValueError("Event {} already registered".format(event))
        self._handlers[event] = EventHandler(event, params, self._loop)

    def unregister(self, event):
        '''
        Remove all registered handlers for an event.
        Silent return when event was not registered.

        Usage:

            dispatch.unregister("my_event")
            dispatch.unregister("my_event")  # no-op

        '''
        self._handlers.pop(event, None)

    async def start(self):
        '''
        Begin processing events.

        If currently shutting down, waits until shutdown completes to start.
        '''
        # no-op if already running
        if self.running:
            return

        # Start during/after shutdown
        if self._start_shutdown.is_set():
            if not self._shutdown_complete.is_set():
                await self._shutdown_complete.wait()
            self._start_shutdown.clear()
            self._shutdown_complete.clear()
            self._resume_processing.clear()

        self.running = True
        for handler in self._handlers.values():
            await handler.start()
        self._loop.create_task(self._run())

    def clear(self):
        '''
        Clear any enqueued events.

        Throws a RuntimeException if called while the Dispatcher is running
        '''
        if self.running:
            raise RuntimeError("Can't clear the queue while running")
        while self.events:
            self._queue.get_nowait()

    @property
    def events(self):
        ''' Number of events currently enqueued '''
        return self._queue.qsize()

    async def stop(self):
        '''
        Stop processing events.

        Yields when all ongoing events have finished.
        '''
        # Bail early if we're not running
        if not self.running:
            return

        self.running = False

        # Signal that the queue should no longer be processed
        self._start_shutdown.set()

        # If the processor is waiting, resume so it can exit cleanly
        self._resume_processing.set()

        # Give all the handlers a chance to complete their pending tasks
        tasks = [handler.stop() for handler in self._handlers.values()]
        if tasks:
            await asyncio.wait(tasks, loop=self._loop)

        # Wait until the queue processor signals back that it's shut down
        await self._shutdown_complete.wait()

    async def _run(self):
        ''' Main queue processor '''
        while self.running:
            if self.events:
                event, params = await self._queue.get()
                handler = self._handlers.get(event, noop)
                handler(params)
            else:
                # Resume on either the next `trigger` call or a `stop`
                await self._resume_processing.wait()
                self._resume_processing.clear()

        # Let the shutdown process continue
        self._shutdown_complete.set()


class EventHandler(object):
    def __init__(self, event, params, loop):
        self._event = event
        self._params = params
        self._callbacks = []
        self._loop = loop
        self.running = False
        self._start_shutdown = asyncio.Event(loop=self._loop)
        self._shutdown_complete = asyncio.Event(loop=self._loop)
        self._tasks = {}

    def __call__(self, params):
        # Don't handle the call if we're shutting down
        if not self.running:
            raise RuntimeError(
                "EventHandler must be running to delegate events")

        for callback in self._callbacks:
            task = self._loop.create_task(callback(params))
            self._tasks[id(task)] = task
            task.add_done_callback(self._task_done_callback)

    def _task_done_callback(self, fut):
        '''
        When a callback is complete, remove it from the active task set.

        Don't throw if the task has already been removed
        '''
        self._tasks.pop(id(fut), None)

    def register(self, callback):
        self._validate(callback)
        wrapped = self._wrap(callback)
        self._callbacks.append(wrapped)
        return callback

    async def start(self):
        '''
        Begin processing events.

        If currently shutting down, waits until shutdown completes to start.
        '''
        # no-op if already running
        if self.running:
            return

        # Start during/after shutdown
        if self._start_shutdown.is_set():
            if not self._shutdown_complete.is_set():
                await self._shutdown_complete.wait()
            self._start_shutdown.clear()
            self._shutdown_complete.clear()

        self.running = True

    async def stop(self):
        ''' Give incomplete tasks a chance to clean up. '''
        # Don't clean up if stop has already been called - processed tasks
        # are already being cleaned up.
        if not self.running:
            return

        self.running = False

        # Signal that events should no longer be processed
        self._start_shutdown.set()

        # Give all active tasks a chance to complete
        active_tasks = list(self._tasks.values())
        if active_tasks:
            await asyncio.wait(active_tasks, loop=self._loop)

        self._shutdown_complete.set()

    def _wrap(self, callback):
        return partial_bind(callback)

    def _validate(self, callback):
        validate_func(self._event, callback, self._params)

    def __repr__(self):
        return "EventHandler({})".format(self._event)


def validate_func(event, callback, params):
    sig = inspect.signature(callback)
    expected = set(sig.parameters)
    for param in sig.parameters.values():
        kind = param.kind
        if kind == inspect.Parameter.VAR_POSITIONAL:
            raise ValueError(
                ("function '{}' expects parameter {} to be VAR_POSITIONAL, "
                 "when it will always be a single value.  This parameter "
                 "must be either POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, or "
                 "KEYWORD_ONLY (or omitted)").format(callback.__name__,
                                                     param.name))
        if kind == inspect.Parameter.VAR_KEYWORD:
            # **kwargs are ok, as long as the **name doesn't
            # mask an actual param that the event emits.
            if param.name in params:
                # masking :(
                raise ValueError(
                    ("function '{}' expects parameter {} to be VAR_KEYWORD, "
                     "which masks an actual parameter for event {}.  This "
                     "event has the following parameters, which must not be "
                     "used as the **VAR_KEYWORD argument.  They may be "
                     "omitted").format(
                        callback.__name__, param.name, event, params))
            else:
                # Pop from expected, this will gobble up any unused params
                expected.remove(param.name)

    available = set(params)
    unavailable = expected - available
    if unavailable:
        raise ValueError(
            ("function '{}' expects the following parameters for event {} "
             "that are not available: {}.  Available parameters for this "
             "event are: {}").format(callback.__name__, event,
                                     unavailable, available))


def partial_bind(callback):
    sig = inspect.signature(callback)
    # Wrap non-coroutines so we can always `await callback(**kw)`
    if not inspect.iscoroutinefunction(callback):
        callback = asyncio.coroutine(callback)
    base = {}
    for key, param in sig.parameters.items():
        default = param.default
        #  Param has no default - use equivalent of empty
        if default is inspect.Parameter.empty:
            base[key] = None
        else:
            base[key] = default

    async def wrapper(params):
        unbound = base.copy()
        # Only map params this callback expects
        for key in base:
            new_value = params.get(key, missing)
            if new_value is not missing:
                unbound[key] = new_value
        bound = sig.bind(**unbound)
        return await callback(*bound.args, **bound.kwargs)

    return wrapper
