
import unittest
import os
import psutil
import time
import threading
from unittest.mock import MagicMock, patch
from gantry.session import DicomSession
from gantry.io_handlers import DicomExporter

class TestMemoryProfile(unittest.TestCase):
    """
    Test to profile memory usage during export to verify worker recycling works.
    Real memory leaks are hard to catch in small unit tests, so this mocks the structure
    and verifies that maxtasksperchild is correctly passed and used.
    """

    @patch('gantry.session.DicomExporter.export_batch')
    def test_export_uses_recycling(self, mock_export_batch):
        # Setup
        mock_export_batch.return_value = 100

        # Init Session (mocking heavy dependencies)
        with patch('gantry.session.SqliteStore'), patch('gantry.session.PersistenceManager'), patch('os.path.exists', return_value=False):
            sess = DicomSession("dummy.db")

            # Setup dummy data
            mock_p = MagicMock(patient_id="P1")
            mock_p.studies = [MagicMock(series=[MagicMock(instances=[MagicMock()])])]
            sess.store.patients = [mock_p]

            # Execute Export
            sess.export("dummy_out")

            # Assert that export_batch was called with maxtasksperchild=10
            mock_export_batch.assert_called_once()
            args, kwargs = mock_export_batch.call_args
            self.assertEqual(kwargs.get('maxtasksperchild'), 10)
            print("\nVerified: DicomSession.export passed maxtasksperchild=10 to export_batch.")

    def test_run_parallel_recycling_integration(self):
        """
        Integration test verifying run_parallel actually switches to multiprocessing.Pool
        when maxtasksperchild is set.
        """
        from gantry.parallel import run_parallel
        import multiprocessing

        # Track active children to permit counting
        # We run a simple function that sleeps briefly

        def worker_func(x):
            return x * x

        # We hook multiprocessing.Pool to verify it's instantiated
        # Note: gantry.parallel imports multiprocessing, so we patch gantry.parallel.multiprocessing.Pool
        with patch('gantry.parallel.multiprocessing.Pool') as mock_pool_cls:
            mock_context = MagicMock()
            mock_pool_cls.return_value.__enter__.return_value = mock_context
            mock_context.imap.return_value = [1, 4, 9]

            items = [1, 2, 3]
            # Force Process Mode to ensure we hit the Maxtasksperchild logic (Free-threaded defaults to Threads)
            with patch.dict(os.environ, {"GANTRY_FORCE_PROCESSES": "1"}):
                # Use max_workers=1 explicitly to avoid os.cpu_count logic clutter
                results = run_parallel(worker_func, items, maxtasksperchild=1, max_workers=1)

            # Verify Pool was initialized with maxtasksperchild=1
            mock_pool_cls.assert_called_once()
            _, kwargs = mock_pool_cls.call_args
            self.assertEqual(kwargs['maxtasksperchild'], 1)
            print("\nVerified: run_parallel initialized multiprocessing.Pool with maxtasksperchild=1.")

if __name__ == "__main__":
    unittest.main()
