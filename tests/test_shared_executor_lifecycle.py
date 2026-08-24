import unittest
import concurrent.futures
from unittest.mock import MagicMock, patch
from gantry.session import DicomSession
from gantry.parallel import run_parallel
import time
import os

class TestSharedExecutorLifecycle(unittest.TestCase):
    def setUp(self):
        self.db_path = ":memory:"
        self.session = DicomSession(self.db_path)

    def tearDown(self):
        if hasattr(self, 'session'):
            self.session.close()

    def test_executor_initialized(self):
        """Verify that the executor is initialized in the constructor."""
        self.assertIsNotNone(self.session._executor)
        self.assertIsInstance(self.session._executor, concurrent.futures.ProcessPoolExecutor)
        # Verify it's running (not shutdown) - submitting a simple task
        future = self.session._executor.submit(sum, [1, 2])
        self.assertEqual(future.result(), 3)

    def test_executor_shutdown(self):
        """Verify that close() shuts down the executor."""
        executor = self.session._executor
        self.session.close()

        # Verify shutdown
        with self.assertRaises(RuntimeError):
            executor.submit(sum, [1, 2])

    @patch('gantry.io_handlers.run_parallel')
    @patch('gantry.session.DicomSession.save')
    def test_export_uses_fresh_recycled_pool_not_shared_executor(self, mock_save, mock_run_parallel):
        """Verify that export builds its own recycling pool instead of reusing the shared executor.

        ProcessPoolExecutor (the shared self._executor used by ingest) doesn't support
        worker recycling, so long export batches would leak memory across workers if they
        reused it. Export must always pass its own maxtasksperchild-bounded pool to
        run_parallel rather than the shared executor.
        """
        mock_run_parallel.return_value = []

        # Construct Object Graph to make total_instances > 0
        p = MagicMock()
        p.patient_id = "P1"
        st = MagicMock()
        se = MagicMock()
        inst = MagicMock()
        inst.instance_number = 1
        inst.sop_instance_uid = "1.2.3.4.5"

        # Link them
        p.studies = [st]
        st.series = [se]
        se.instances = [inst]

        # Add to store
        self.session.store.patients.append(p)

        # Act
        self.session.export("out_folder", safe=False)

        # Assert
        self.assertTrue(mock_run_parallel.called)
        args, kwargs = mock_run_parallel.call_args

        # Export must request a recycled pool (bounds memory growth over long batches).
        self.assertIn('maxtasksperchild', kwargs)
        self.assertEqual(kwargs['maxtasksperchild'], 25)

        # ...and that pool must NOT be the shared, non-recycling self._executor.
        passed_executor = kwargs.get('executor')
        self.assertNotEqual(passed_executor, self.session._executor)

    @patch('gantry.io_handlers.run_parallel')
    @patch('os.path.isfile')
    @patch('os.path.isdir')
    def test_ingest_uses_executor(self, mock_isdir, mock_isfile, mock_run_parallel):
        """Verify that ingest passes the executor to run_parallel."""
        # Setup mock behavior
        mock_isfile.return_value = True # Pretend it's a file
        mock_isdir.return_value = False

        mock_run_parallel.return_value = []

        # Act
        self.session.ingest("dummy_file.dcm")

        # Assert
        # Check that run_parallel was called with executor=self.session._executor
        # We need to ensure new_files was populated. Reference io_handlers.py
        # DicomStore.get_known_files returns set(). Defaults are fine.

        self.assertTrue(mock_run_parallel.called)
        args, kwargs = mock_run_parallel.call_args
        self.assertIn('executor', kwargs)
        self.assertEqual(kwargs['executor'], self.session._executor)

    @patch('gantry.io_handlers.run_parallel')
    @patch('os.path.isfile')
    def test_consistency_across_calls(self, mock_isfile, mock_run):
        """Verify that the same executor is reused across multiple calls."""
        mock_isfile.return_value = True
        mock_run.return_value = []

        self.session.ingest("file1")
        exec1 = mock_run.call_args[1].get('executor')

        self.session.ingest("file2")
        exec2 = mock_run.call_args[1].get('executor')

        self.assertEqual(exec1, exec2)
        self.assertEqual(exec1, self.session._executor)

if __name__ == '__main__':
    unittest.main()
