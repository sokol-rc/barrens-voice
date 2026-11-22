import hashlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bv import trt_runtime


class TensorRTRuntime(unittest.TestCase):
    def test_verified_wheel_is_installed_once(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as wheel:
            for dll in (*trt_runtime.DLLS, "builder_resource.dll"):
                wheel.writestr(f"tensorrt_libs/{dll}", b"test")
            wheel.writestr("metadata/version", b"test")
        data = buffer.getvalue()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(trt_runtime, "SHA256", hashlib.sha256(data).hexdigest()),
            patch.object(trt_runtime.urllib.request, "urlopen", return_value=io.BytesIO(data)) as fetch,
        ):
            root = Path(directory)
            installed = trt_runtime.runtime(root, lambda _: None)
            self.assertEqual((installed / "builder_resource.dll").read_bytes(), b"test")
            self.assertEqual(trt_runtime.runtime(root, lambda _: None), installed)
            self.assertFalse((installed.parent / "metadata").exists())
            fetch.assert_called_once()

    def test_failed_download_does_not_publish_a_runtime(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                trt_runtime.urllib.request, "urlopen", return_value=io.BytesIO(b"damaged")):
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
                trt_runtime.runtime(root, lambda _: None)
            self.assertEqual(list((root / "tensorrt").iterdir()), [])
