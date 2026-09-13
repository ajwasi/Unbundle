import logging

from app import applog


def test_recent_lines_empty_by_default():
    applog.clear()
    assert applog.recent_lines() == []
    assert applog.as_text() == ""


def test_install_captures_root_logger_records():
    applog.clear()
    applog.install()
    logging.getLogger("some.module").info("hello %s", "world")
    lines = applog.recent_lines()
    assert any("hello world" in line for line in lines)


def test_install_is_idempotent():
    applog.install()
    applog.install()
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, applog._RingBufferHandler)]
    assert len(handlers) == 1


def test_as_text_joins_lines_with_trailing_newline():
    applog.clear()
    applog.install()
    logging.getLogger("x").info("one")
    logging.getLogger("x").info("two")
    text = applog.as_text()
    assert text.endswith("\n")
    assert "one" in text and "two" in text


def test_clear_empties_buffer():
    applog.install()
    logging.getLogger("x").info("something")
    assert applog.recent_lines()
    applog.clear()
    assert applog.recent_lines() == []


def test_download_filename_has_txt_extension():
    assert applog.download_filename().endswith(".txt")
