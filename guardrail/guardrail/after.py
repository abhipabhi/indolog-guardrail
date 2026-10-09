"""Work that happens after an answer has been given: the shadow checks a policy puts there.

One worker thread and a bounded queue, on purpose. The first version handed each request's
deferred checks to the web framework's background tasks; under back-to-back requests they ran all
at once, fought over the CPUs, and made the answers they were supposed not to delay slower than
before (measured: p95 from 295 ms to 985 ms). One at a time, they cost a request nothing but a
share of the CPU.

When the queue is full the work is not done: the decision is still written down, at once, with
those checks marked as not observed. Nothing an observer does or fails to do changes an answer.
"""
from __future__ import annotations

import contextvars
import logging
import queue
import threading
from typing import Callable

log = logging.getLogger("guardrail")


class AfterResponse:
    def __init__(self, max_waiting: int = 256) -> None:
        self.not_observed = 0
        self._queue: queue.Queue = queue.Queue(maxsize=max_waiting)
        threading.Thread(target=self._work, name="guardrail-after-response", daemon=True).start()

    def submit(self, complete: Callable[[bool], None]) -> None:
        """`complete(True)` will be called on the worker thread; if there is no room to wait,
        `complete(False)` is called here and now instead."""
        # The work carries the request's context with it (its trace parent, for one), since it will
        # run on another thread after the request has ended.
        context = contextvars.copy_context()
        try:
            self._queue.put_nowait(lambda observe: context.run(complete, observe))
        except queue.Full:
            self.not_observed += 1
            if self.not_observed == 1 or self.not_observed % 100 == 0:
                log.error("the after-response queue is full: %s request(s) not observed by their shadow checks",
                          self.not_observed)
            complete(False)

    def _work(self) -> None:
        while True:
            complete = self._queue.get()
            try:
                complete(True)
            except Exception as exc:    # noqa: BLE001 -- an observer; the answer was given long ago
                log.error("after-response work raised %s", type(exc).__name__)
            finally:
                self._queue.task_done()

    def flush(self) -> None:
        """Waits until everything submitted has run. For tests, the evaluation and shutdown."""
        self._queue.join()
