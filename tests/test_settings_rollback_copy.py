"""Going back to v3.3.0 after Jellyfin paths are saved (docs/media-servers-design.md).

v3.3.0 and earlier rebuild each path mapping from a fixed list of fields when
they save the settings file, which every run does. They drop the Jellyfin
fields, and their loader turns an empty plex_path (a library only Jellyfin has)
into "/", a prefix every Plex path matches. So before the first save that adds
something they would mishandle, the Web UI keeps the file as it was, byte for
byte, as data/plexcache_settings.before-media-servers.json: the file to put
back before going back.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault('fcntl', MagicMock())
for _mod in ['apscheduler', 'apscheduler.schedulers', 'apscheduler.schedulers.background',
             'apscheduler.triggers', 'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
             'plexapi', 'plexapi.server', 'plexapi.myplex']:
    sys.modules.setdefault(_mod, MagicMock())

from core.config import older_versions_mishandle  # noqa: E402

V330_MAPPING = {"name": "Movies", "plex_path": "/data/movies/", "real_path": "/mnt/user/movies/",
                "cache_path": "/mnt/cache/movies/", "host_cache_path": "/mnt/cache/movies/",
                "cacheable": True, "enabled": True, "section_id": 1}
V330 = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "tok", "cache_dir": "/mnt/cache/",
        "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "key",
        "jellyfin_path_mappings": {"/media/movies": "/data/movies"}, "path_mappings": [V330_MAPPING]}
WITH_JELLYFIN_PATH = {**V330, "path_mappings": [{**V330_MAPPING, "jellyfin_path": "/media/movies"}]}
COPY = "plexcache_settings.before-media-servers.json"


def test_what_older_versions_mishandle():
    assert not older_versions_mishandle(V330)
    assert not older_versions_mishandle({"path_mappings": [{"name": "Old", "plex_path": "/data/", "real_path": "/x/"}]})
    assert older_versions_mishandle(WITH_JELLYFIN_PATH)
    assert older_versions_mishandle({"path_mappings": [{**V330_MAPPING, "plex_path": ""}]})  # Jellyfin-only library
    assert older_versions_mishandle({"path_mappings": [{**V330_MAPPING, "future_field": 1}]})
    assert not older_versions_mishandle({})


@pytest.fixture
def service(tmp_path):
    from web.services.settings_service import SettingsService
    settings_file = tmp_path / "plexcache_settings.json"
    # As v3.3.0 writes it: indent=4
    settings_file.write_text(json.dumps(V330, indent=4), encoding="utf-8")
    svc = SettingsService.__new__(SettingsService)
    svc.settings_file = settings_file
    svc._cached_settings = None
    data = tmp_path / "data"
    with patch("web.services.settings_service.DATA_DIR", data):
        yield svc, settings_file, data / COPY


def test_saves_older_versions_read_fine_make_no_copy(service):
    svc, _, copy = service
    assert svc._save_raw({**V330, "cache_limit": "500GB"})
    assert not copy.exists()


@pytest.mark.critical
def test_the_first_jellyfin_path_keeps_the_file_as_it_was(service):
    svc, settings_file, copy = service
    before = settings_file.read_bytes()
    assert svc._save_raw(WITH_JELLYFIN_PATH)
    assert copy.read_bytes() == before  # byte for byte, as v3.3.0 left it
    assert json.loads(settings_file.read_text())["path_mappings"][0]["jellyfin_path"] == "/media/movies"

    # Later saves leave the copy alone
    assert svc._save_raw({**WITH_JELLYFIN_PATH, "path_mappings": [
        {**V330_MAPPING, "jellyfin_path": "/media/films", "jellyfin_library_id": "lib"}]})
    assert copy.read_bytes() == before


def test_a_file_that_already_has_the_new_fields_has_nothing_to_keep(service):
    svc, settings_file, copy = service
    settings_file.write_text(json.dumps(WITH_JELLYFIN_PATH), encoding="utf-8")
    assert svc._save_raw({**WITH_JELLYFIN_PATH, "cache_limit": "1TB"})
    assert not copy.exists()


def test_a_new_install_has_nothing_to_keep(service):
    """The setup wizard's first save of a Jellyfin-only install: no older file to go back to."""
    svc, settings_file, copy = service
    settings_file.write_text(json.dumps({"firststart": True}), encoding="utf-8")
    assert svc._save_raw({"path_mappings": [{**V330_MAPPING, "plex_path": "", "jellyfin_path": "/media/movies"}]})
    assert not copy.exists()


def test_a_failed_copy_doesnt_stop_the_save(service):
    svc, settings_file, copy = service
    with patch("web.services.settings_service.shutil.copy2", side_effect=OSError("disk full")):
        assert svc._save_raw(WITH_JELLYFIN_PATH)
    assert json.loads(settings_file.read_text())["path_mappings"][0]["jellyfin_path"] == "/media/movies"
