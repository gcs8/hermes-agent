"""Patch-budget inspection keeps its soft fallback and local debug traceback."""

import logging
import sys
from unittest.mock import MagicMock

import pytest
from PIL import Image

from tools.vision_tools_image_prep import _analysis_image_dimension_limit


@pytest.mark.parametrize("error", [OSError("unreadable raster"), RuntimeError("decoder failed")])
def test_inspection_failure_preserves_default_limit_and_debug_traceback(monkeypatch, tmp_path, caplog, error):
    def fail(_path):
        raise error

    monkeypatch.setattr(Image, "open", fail)
    with caplog.at_level(logging.DEBUG, logger="tools.vision_tools"):
        assert _analysis_image_dimension_limit(tmp_path / "synthetic.png") == 8000
    record, = [r for r in caplog.records if r.name == "tools.vision_tools"]
    assert record.levelno == logging.DEBUG
    assert record.exc_info[1] is error
    assert record.exc_info[2] is not None
    assert record.getMessage() == "Image patch-budget inspection failed; using default dimension limit"
    assert not record.args


def test_missing_pillow_preserves_default_limit(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(sys.modules, "PIL", None)
    with caplog.at_level(logging.DEBUG, logger="tools.vision_tools"):
        assert _analysis_image_dimension_limit(tmp_path / "synthetic.png") == 8000
    assert any(r.exc_info and isinstance(r.exc_info[1], ImportError) for r in caplog.records)


@pytest.mark.parametrize("size, expected", [((100, 100), 8000), ((8000, 8000), 5248)])
def test_success_preserves_patch_budget_limit(monkeypatch, tmp_path, caplog, size, expected):
    opened = MagicMock()
    opened.__enter__.return_value.size = size
    monkeypatch.setattr(Image, "open", lambda _: opened)
    with caplog.at_level(logging.DEBUG, logger="tools.vision_tools"):
        assert _analysis_image_dimension_limit(tmp_path / "synthetic.png") == expected
    assert not [r for r in caplog.records if r.name == "tools.vision_tools"]


def test_inspection_does_not_swallow_process_interrupt(monkeypatch, tmp_path):
    def interrupt(_path):
        raise KeyboardInterrupt

    monkeypatch.setattr(Image, "open", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _analysis_image_dimension_limit(tmp_path / "synthetic.png")
