import collections
import os
import unittest
from unittest.mock import patch
from isocenter import parallel

def identity(x):
    return x

class TestParallelConfig(unittest.TestCase):

    def setUp(self):
        # Save original environ
        self.original_environ = os.environ.copy()
        # Force processes to ensure we test ProcessPoolExecutor logic by default
        # (Since free-threaded Python defaults to threads)
        os.environ["ISOCENTER_FORCE_PROCESSES"] = "1"
        if "ISOCENTER_FORCE_THREADS" in os.environ:
            del os.environ["ISOCENTER_FORCE_THREADS"]

    def tearDown(self):
        # Restore original environ to prevent side effects
        os.environ.clear()
        os.environ.update(self.original_environ)

    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    def test_run_parallel_max_workers_env(self, mock_executor):
        """Test that ISOCENTER_MAX_WORKERS controls the number of workers."""
        os.environ["ISOCENTER_MAX_WORKERS"] = "42"

        # Mock context manager
        mock_instance = mock_executor.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.map.return_value = [1, 2, 3]

        parallel.run_parallel(identity, [1, 2, 3], show_progress=False)

        # The subject is the worker count; asserting the whole signature
        # made this fail for the unrelated spawn pin (#220).
        assert mock_executor.call_args.kwargs["max_workers"] == 42

    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    def test_run_parallel_chunksize_env(self, mock_executor):
        """Test that ISOCENTER_CHUNKSIZE is respected."""
        os.environ["ISOCENTER_CHUNKSIZE"] = "5"

        # Setup mock
        mock_instance = mock_executor.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.map.return_value = [1, 2, 3]

        parallel.run_parallel(identity, [1, 2, 3], show_progress=False)

        # Verify map was called with chunksize=5
        mock_instance.map.assert_called_with(identity, [1, 2, 3], chunksize=5)

    # Every mocked-pool test below patches BOTH constructors and asserts
    # the one it does not expect was never called. With only the expected
    # one patched, a mutant that reroutes the dispatch -- line 259's
    # `if maxtasksperchild is None` -> `is not None` sends this call down
    # the *unpatched* ProcessPoolExecutor path with a Mock for its
    # mp_context -- parks `Future.result()` on a queue that is a
    # MagicMock and hangs pytest until the probe's 900 s timeout (#365,
    # spec §3.3). Patching both turns that mutant into a red assertion in
    # milliseconds. The recycling pool is `parallel._RecyclingPool` (#887),
    # patched whole: a real pool handed a Mock context, as patching
    # `multiprocessing.get_context` did when the pool was `ctx.Pool()`,
    # builds MagicMock queues whose `poll()` is always truthy, and its
    # `_help_stuff_finish` then spins for good on the exit's helper thread.

    @staticmethod
    def _recycling_pool_returning(mock_pool_class, results):
        """Make the patched `_RecyclingPool` yield `results` from
        `imap_unordered`, with the fields the stream's watch reads."""
        mock_pool = mock_pool_class.return_value
        mock_pool._started = collections.deque()
        mock_pool._pool = []
        mock_pool._exit_grace = parallel._WORKER_EXIT_GRACE_S

        class MockIterator:
            """`IMapIterator`'s `next(timeout=)`, over a list."""
            def __init__(self, items):
                self._iter = iter(items)
            def next(self, timeout=None):  # pylint: disable=unused-argument
                return next(self._iter)

        mock_pool.imap_unordered.return_value = MockIterator(results)
        return mock_pool

    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    @patch('isocenter.parallel._RecyclingPool')
    def test_run_parallel_maxtasksperchild(self, mock_pool_class, mock_executor):
        """Test that ISOCENTER_MAX_TASKS_PER_CHILD triggers the recycling pool."""
        os.environ["ISOCENTER_MAX_TASKS_PER_CHILD"] = "10"
        self._recycling_pool_returning(mock_pool_class, [1, 2, 3])

        self.assertEqual(sorted(parallel.run_parallel(
            identity, [1, 2, 3], show_progress=False)), [1, 2, 3])

        # check that Pool was initialized with maxtasksperchild=10
        mock_pool_class.assert_called()
        mock_executor.assert_not_called()
        call_kwargs = mock_pool_class.call_args[1]
        self.assertEqual(call_kwargs.get('maxtasksperchild'), 10)

    def _assert_disables_gc(self, initializer):
        """The initializer is the resolved `_worker_init` with GC off.

        Asserted on the partial's own contents rather than by calling
        it: calling would disable this process's collector. The partial
        shape is itself part of the contract -- settings must travel as
        pickled arguments, because a spawned child re-imports the module
        fresh and an argument-driven `disable_gc=True` (no env var set
        in the child-visible sense) would otherwise be lost.
        """
        self.assertIsNotNone(initializer)
        self.assertIs(initializer.func, parallel._worker_init)
        self.assertTrue(initializer.keywords.get('disable_gc'))

    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    @patch('isocenter.parallel._RecyclingPool')
    def test_run_parallel_disable_gc_maxtasks(self, mock_pool_class, mock_executor):
        """Test ISOCENTER_DISABLE_GC with maxtasksperchild path.

        This is the test that hung for 900 s under the line-259 mutant
        before both constructors were patched (spec §3.3); with the
        executor patched too, that mutant is the `assert_not_called`
        below going red.
        """
        os.environ["ISOCENTER_MAX_TASKS_PER_CHILD"] = "5"
        os.environ["ISOCENTER_DISABLE_GC"] = "1"
        self._recycling_pool_returning(mock_pool_class, [1])

        parallel.run_parallel(identity, [1], show_progress=False)

        mock_pool_class.assert_called()
        mock_executor.assert_not_called()
        call_kwargs = mock_pool_class.call_args[1]
        self._assert_disables_gc(call_kwargs.get('initializer'))

    @patch('isocenter.parallel._RecyclingPool')
    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    def test_run_parallel_disable_gc_executor(self, mock_executor, mock_pool_class):
        """Test ISOCENTER_DISABLE_GC with standard ProcessPoolExecutor."""
        os.environ["ISOCENTER_DISABLE_GC"] = "1"
        # Ensure we don't trigger maxtasks path
        if "ISOCENTER_MAX_TASKS_PER_CHILD" in os.environ:
            del os.environ["ISOCENTER_MAX_TASKS_PER_CHILD"]

        mock_instance = mock_executor.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.map.return_value = [1]

        parallel.run_parallel(identity, [1], show_progress=False)

        mock_executor.assert_called()
        mock_pool_class.assert_not_called()
        call_kwargs = mock_executor.call_args[1]
        self._assert_disables_gc(call_kwargs.get('initializer'))

    @patch('isocenter.parallel._RecyclingPool')
    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    def test_disable_gc_as_an_argument_alone_reaches_the_initializer(
            self, mock_executor, mock_pool_class):
        """`disable_gc=True` with no environment variable set (#365).

        Every existing test set `ISOCENTER_DISABLE_GC=1`, and the
        variable is folded in twice -- `_resolve_strategy` and
        `resolve_worker_initializer` each do `disable_gc or _env_is(...)`
        -- so either `or` could become `and` and the other would rescue
        it. The argument-only path is the one that tells them apart:
        with the variable unset, an `and` at either site folds to False
        and the pool gets no initializer. Killing mutations: `or -> and`
        at both sites.
        """
        for name in ("ISOCENTER_DISABLE_GC", "ISOCENTER_MAX_TASKS_PER_CHILD",
                     "ISOCENTER_WORKER_FAULTHANDLER"):
            os.environ.pop(name, None)

        mock_instance = mock_executor.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.map.return_value = [1]

        parallel.run_parallel(identity, [1], show_progress=False,
                              disable_gc=True)

        mock_executor.assert_called()
        mock_pool_class.assert_not_called()
        call_kwargs = mock_executor.call_args[1]
        self._assert_disables_gc(call_kwargs.get('initializer'))

    @patch('isocenter.parallel._RecyclingPool')
    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    def test_without_any_lever_run_parallel_hands_the_pool_no_initializer(
            self, mock_executor, mock_pool_class):
        """`run_parallel`'s own `disable_gc=False` default is exercised (#365).

        `test_without_the_env_var_no_initializer_is_forced_on_workers`
        in `test_parallel_contract.py` calls `_resolve_strategy` directly
        with `False`, so `run_parallel`'s default was never read by any
        test and could be flipped to `True` unnoticed -- every worker
        would then run with its collector off. Killing mutation:
        `disable_gc: bool = False` -> `True` in `run_parallel`'s
        signature.
        """
        for name in ("ISOCENTER_DISABLE_GC", "ISOCENTER_MAX_TASKS_PER_CHILD",
                     "ISOCENTER_WORKER_FAULTHANDLER"):
            os.environ.pop(name, None)

        mock_instance = mock_executor.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.map.return_value = [1]

        parallel.run_parallel(identity, [1], show_progress=False)

        mock_executor.assert_called()
        call_kwargs = mock_executor.call_args[1]
        self.assertIsNone(call_kwargs.get('initializer'))

    @patch('isocenter.parallel.tqdm')
    def test_progress_is_on_by_default(self, mock_tqdm):
        """A bar is drawn when nobody said otherwise (#365).

        The `ISOCENTER_SHOW_PROGRESS` row documents the default as `1`,
        and every test passes `show_progress` explicitly to keep the
        output quiet, so `run_parallel`'s own default was pinned by
        nothing. Threads, so the call spawns no process; the bar is a
        Mock, so nothing is drawn here either. Killing mutation:
        `show_progress: bool = True` -> `False` in the signature.
        """
        os.environ.pop("ISOCENTER_SHOW_PROGRESS", None)
        os.environ.pop("ISOCENTER_FORCE_PROCESSES", None)
        os.environ["ISOCENTER_FORCE_THREADS"] = "1"
        mock_tqdm.return_value = iter([1, 2])

        parallel.run_parallel(identity, [1, 2])

        mock_tqdm.assert_called_once()

    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    @patch('isocenter.parallel.tqdm')
    def test_run_parallel_show_progress_env(self, mock_tqdm, mock_executor):
        """Test that ISOCENTER_SHOW_PROGRESS=0 disables tqdm."""
        os.environ["ISOCENTER_SHOW_PROGRESS"] = "0"

        mock_instance = mock_executor.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.map.return_value = [1]

        # Pass show_progress=True explicitly
        parallel.run_parallel(identity, [1], show_progress=True)

        # tqdm should NOT be called
        mock_tqdm.assert_not_called()

