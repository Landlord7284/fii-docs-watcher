"""Configuration discovery, coercion and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from fii_docs_watcher.clock import DEFAULT_TIMEZONE, set_timezone, source_tz
from fii_docs_watcher.config import (
    CONFIG_SEARCH_PATH,
    ENV_CONFIG_PATH,
    MAX_PAGE_LENGTH,
    describe_source,
    discover,
    load,
)
from fii_docs_watcher.errors import ConfigError


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run each test in an empty directory with no config anywhere.

    Without this, discovery would find the repository's own `./config.toml` and
    the results would depend on where pytest was started from.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    monkeypatch.setattr(
        "fii_docs_watcher.config.CONFIG_SEARCH_PATH",
        (Path("config.toml"), Path("fii-docs-watcher.toml"), tmp_path / "nonexistent-home.toml"),
    )
    return tmp_path


class TestDiscovery:
    def test_nothing_found_falls_back_to_defaults(self) -> None:
        assert discover() is None
        config = load()
        assert config.source_path is None
        assert config.paths.data_root == Path("./var/data")

    def test_the_fallback_is_described_rather_than_silent(self) -> None:
        # The defaults point at ./var/..., so a user with a config elsewhere
        # would otherwise be writing to a different archive without knowing.
        described = describe_source(load())
        assert "built-in defaults" in described
        assert "config.toml" in described

    def test_a_config_in_the_working_directory_is_found(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text("[retention]\ndays = 3\n")
        config = load()
        assert config.source_path == Path("config.toml")
        assert config.retention.days == 3

    def test_the_second_filename_is_also_searched(self, isolated: Path) -> None:
        (isolated / "fii-docs-watcher.toml").write_text("[retention]\ndays = 4\n")
        assert load().retention.days == 4

    def test_the_first_search_entry_wins(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text("[retention]\ndays = 3\n")
        (isolated / "fii-docs-watcher.toml").write_text("[retention]\ndays = 9\n")
        assert load().retention.days == 3

    def test_the_environment_beats_the_working_directory(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (isolated / "config.toml").write_text("[retention]\ndays = 3\n")
        (isolated / "elsewhere.toml").write_text("[retention]\ndays = 5\n")
        monkeypatch.setenv(ENV_CONFIG_PATH, str(isolated / "elsewhere.toml"))
        assert load().retention.days == 5

    def test_an_explicit_path_beats_everything(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (isolated / "config.toml").write_text("[retention]\ndays = 3\n")
        (isolated / "elsewhere.toml").write_text("[retention]\ndays = 5\n")
        (isolated / "explicit.toml").write_text("[retention]\ndays = 7\n")
        monkeypatch.setenv(ENV_CONFIG_PATH, str(isolated / "elsewhere.toml"))
        assert load(isolated / "explicit.toml").retention.days == 7

    def test_an_explicitly_named_missing_file_is_an_error(self) -> None:
        # Naming a file and not finding it is a mistake, never a cue to use
        # different settings.
        with pytest.raises(ConfigError, match="not found"):
            load("definitely-not-here.toml")

    def test_a_missing_file_named_by_the_environment_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_CONFIG_PATH, "/nope/missing.toml")
        with pytest.raises(ConfigError, match="missing file"):
            load()

    def test_invalid_toml_is_reported_with_its_path(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text("this is not = = toml\n")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load()

    def test_the_real_search_path_is_ordered_project_first(self) -> None:
        # Running from a checkout should pick up that checkout's configuration.
        names = [p.name for p in CONFIG_SEARCH_PATH]
        assert names[0] == "config.toml"


class TestFormats:
    def test_pdf_only_by_default(self) -> None:
        # A reading queue for people. The XML is the same filing in a
        # machine-readable shape, and Pipeline B fetches its own.
        config = load()
        assert config.download.formats == ("pdf",)
        assert not config.download.all_formats
        assert config.download.wants("pdf")
        assert not config.download.wants("xml")

    def test_xml_is_opt_in(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text('[download]\nformats = ["pdf", "xml"]\n')
        download = load().download
        assert download.formats == ("pdf", "xml")
        assert download.all_formats

    def test_a_toml_array_is_read(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text('[download]\nformats = ["xml"]\n')
        download = load().download
        assert download.formats == ("xml",)
        assert download.wants("xml")
        assert not download.wants("pdf")
        assert not download.all_formats

    def test_an_environment_override_accepts_a_comma_separated_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An environment variable can only carry a string.
        monkeypatch.setenv("FII_WATCHER_DOWNLOAD_FORMATS", "PDF, xml")
        assert load().download.formats == ("pdf", "xml")

    def test_a_leading_dot_and_case_do_not_matter_when_asking(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text('[download]\nformats = ["pdf"]\n')
        assert load().download.wants(".PDF")

    def test_an_empty_list_is_rejected(self, isolated: Path) -> None:
        # It would archive nothing at all, which is never what anyone meant.
        (isolated / "config.toml").write_text("[download]\nformats = []\n")
        with pytest.raises(ConfigError, match="empty"):
            load()

    def test_an_unknown_format_is_rejected(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text('[download]\nformats = ["doc"]\n')
        with pytest.raises(ConfigError, match="doc"):
            load()


class TestValidation:
    def test_page_length_above_the_source_ceiling_is_rejected(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text(
            f"[source]\npage_length = {MAX_PAGE_LENGTH + 1}\n"
        )
        with pytest.raises(ConfigError, match="HTTP 500"):
            load()

    def test_zero_retention_is_rejected(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text("[retention]\ndays = 0\n")
        with pytest.raises(ConfigError, match="days"):
            load()

    def test_zero_cvm_response_limit_is_rejected(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text("[cvm]\nmax_response_bytes = 0\n")
        with pytest.raises(ConfigError, match="max_response_bytes"):
            load()

    def test_zero_source_response_limit_is_rejected(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text("[source]\nmax_response_bytes = 0\n")
        with pytest.raises(ConfigError, match="max_response_bytes"):
            load()

    def test_the_two_roots_may_not_be_the_same_or_nested(self, isolated: Path) -> None:
        # The documents root is meant to be shared and the data root must not be.
        (isolated / "config.toml").write_text(
            '[paths]\ndata_root = "./same"\ndocuments_root = "./same"\n'
        )
        with pytest.raises(ConfigError, match="must differ"):
            load()

        (isolated / "config.toml").write_text(
            '[paths]\ndata_root = "./a"\ndocuments_root = "./a/b"\n'
        )
        with pytest.raises(ConfigError, match="nested"):
            load()

    def test_an_unknown_key_is_reported_rather_than_ignored(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text("[retention]\ndayz = 7\n")
        with pytest.raises(ConfigError, match="unknown key"):
            load()

    def test_environment_overrides_apply_to_any_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FII_WATCHER_RETENTION_DAYS", "21")
        monkeypatch.setenv("FII_WATCHER_PATHS_DATA_ROOT", "/tmp/elsewhere")
        config = load()
        assert config.retention.days == 21
        assert config.paths.data_root == Path("/tmp/elsewhere")


class TestTimezone:
    @pytest.fixture(autouse=True)
    def restore_default(self):
        # `load()` installs the zone process-wide, so a test that loads a
        # non-default one would otherwise re-date every later test's fixtures.
        yield
        set_timezone(DEFAULT_TIMEZONE)

    def test_the_default_needs_no_config_file(self) -> None:
        assert load().source.timezone == DEFAULT_TIMEZONE
        assert str(source_tz()) == DEFAULT_TIMEZONE

    def test_loading_installs_the_configured_zone(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text('[source]\ntimezone = "UTC"\n')
        assert load().source.timezone == "UTC"
        # The point of the key: the clock has to actually follow it.
        assert str(source_tz()) == "UTC"

    def test_the_environment_cannot_override_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The config file is the project's single declaration of a zone. A
        # `.env` only exists under compose and is invisible to a native
        # install, so a second declaration there would let two deployments of
        # one archive file the same document under different dates.
        monkeypatch.setenv("FII_WATCHER_SOURCE_TIMEZONE", "Europe/Lisbon")
        with pytest.raises(ConfigError, match="cannot be overridden from the environment"):
            load()

    def test_the_refusal_is_an_error_rather_than_a_silent_ignore(
        self, monkeypatch: pytest.MonkeyPatch, isolated: Path
    ) -> None:
        # A value with no effect is worse than an error: the operator would
        # read Lisbon and get Sao Paulo, with nothing to say why.
        (isolated / "config.toml").write_text('[source]\ntimezone = "UTC"\n')
        monkeypatch.setenv("FII_WATCHER_SOURCE_TIMEZONE", "Europe/Lisbon")
        with pytest.raises(ConfigError) as excinfo:
            load()
        # The message has to name the alternative, or the refusal is a dead end.
        assert "config file" in str(excinfo.value)
        assert str(source_tz()) == DEFAULT_TIMEZONE

    def test_an_unknown_zone_is_a_config_error(self, isolated: Path) -> None:
        (isolated / "config.toml").write_text('[source]\ntimezone = "Mars/Olympus"\n')
        with pytest.raises(ConfigError, match="not an IANA timezone"):
            load()
