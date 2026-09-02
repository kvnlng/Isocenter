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

    @patch('multiprocessing.get_context')
    def test_run_parallel_maxtasksperchild(self, mock_get_context):
        """Test that ISOCENTER_MAX_TASKS_PER_CHILD triggers multiprocessing.Pool."""
        os.environ["ISOCENTER_MAX_TASKS_PER_CHILD"] = "10"

        mock_ctx = mock_get_context.return_value
        mock_pool = mock_ctx.Pool.return_value
        mock_pool.__enter__.return_value = mock_pool

        # Mock iterator with next(timeout) support
        class MockIterator:
            def __init__(self, items):
                self._iter = iter(items)
            def __next__(self):
                return next(self._iter)
            def __iter__(self):
                return self

        mock_pool.imap_unordered.return_value = MockIterator([1, 2, 3])

        parallel.run_parallel(identity, [1, 2, 3], show_progress=False)

        # check that Pool was initialized with maxtasksperchild=10
        mock_ctx.Pool.assert_called()
        call_kwargs = mock_ctx.Pool.call_args[1]
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

    @patch('multiprocessing.get_context')
    def test_run_parallel_disable_gc_maxtasks(self, mock_get_context):
        """Test ISOCENTER_DISABLE_GC with maxtasksperchild path."""
        os.environ["ISOCENTER_MAX_TASKS_PER_CHILD"] = "5"
        os.environ["ISOCENTER_DISABLE_GC"] = "1"

        mock_ctx = mock_get_context.return_value
        mock_pool = mock_ctx.Pool.return_value
        mock_pool.__enter__.return_value = mock_pool

        # Mock iterator
        class MockIterator:
            def __init__(self, items):
                self._iter = iter(items)
            def __next__(self):
                return next(self._iter)
            def __iter__(self):
                return self

        mock_pool.imap_unordered.return_value = MockIterator([1])

        parallel.run_parallel(identity, [1], show_progress=False)

        mock_ctx.Pool.assert_called()
        call_kwargs = mock_ctx.Pool.call_args[1]
        self._assert_disables_gc(call_kwargs.get('initializer'))

    @patch('isocenter.parallel.concurrent.futures.ProcessPoolExecutor')
    def test_run_parallel_disable_gc_executor(self, mock_executor):
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
        call_kwargs = mock_executor.call_args[1]
        self._assert_disables_gc(call_kwargs.get('initializer'))

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

