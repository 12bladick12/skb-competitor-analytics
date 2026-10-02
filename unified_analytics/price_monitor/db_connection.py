"""Client-side I/O deadline, including a lost response after COMMIT.

PostgreSQL statement_timeout cannot end a client waiting on a broken pooler
connection after the server has already finished its transaction.
"""
import time

import psycopg


class DatabaseIOTimeout(psycopg.OperationalError):
    """The commit outcome is unknown: reconcile the durable queue before retry."""


class DeadlineConnection(psycopg.Connection):
    io_timeout = 45.0
    io_deadline = None

    def wait(self, gen, interval=0.1, timeout=None):
        budget = self.io_timeout if timeout is None else min(timeout, self.io_timeout)
        if self.io_deadline is not None:
            budget = min(budget, max(0.001, self.io_deadline-time.monotonic()))
        try:
            return super().wait(gen, interval=interval, timeout=budget)
        except psycopg.errors._WaitTimeout:
            # This runs in the connection's owning thread after polling ended.
            # Do not wait for cancellation/rollback on the same broken socket.
            self.close()
            raise DatabaseIOTimeout('Database response deadline exceeded; reconnect and reconcile queue') from None
