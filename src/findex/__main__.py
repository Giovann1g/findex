"""``python -m findex`` is the same as the ``findex`` command.

The guard matters for ``--executor processes``: with the *spawn* start method
each worker re-imports the parent's main module (as ``__mp_main__``). Without
the guard that import would run the whole CLI again inside every worker.
"""

from findex.cli import app

if __name__ == "__main__":
    app(prog_name="findex")
