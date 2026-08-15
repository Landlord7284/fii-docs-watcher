"""CNPJ normalisation and the timezone-fixed clock."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from fii_docs_watcher.clock import (
    DEFAULT_TIMEZONE,
    parse_delivery,
    parse_dir_name,
    parse_reference,
    retention_window,
    set_timezone,
    source_tz,
    to_dir_name,
)
from fii_docs_watcher.errors import ConfigError, SourceContractError
from fii_docs_watcher.scope import cnpj as cnpj_mod


class TestCnpj:
    @pytest.mark.parametrize(
        "value",
        ["08431747000106", "08.431.747/0001-06", " 08.431.747 / 0001-06 ", "08431747000106\n"],
    )
    def test_every_spelling_normalises_the_same(self, value: str) -> None:
        assert cnpj_mod.normalize(value) == "08431747000106"

    def test_an_integer_regains_the_leading_zero_yaml_would_have_eaten(self) -> None:
        # A CNPJ written unquoted in YAML parses as an int and loses its zero.
        assert cnpj_mod.normalize(8431747000106) == "08431747000106"

    @pytest.mark.parametrize("value", [None, "", "not a cnpj", "123456789012345678"])
    def test_unusable_values_return_none(self, value: object) -> None:
        assert cnpj_mod.normalize(value) is None  # type: ignore[arg-type]

    def test_check_digits(self) -> None:
        assert cnpj_mod.is_valid("08431747000106")
        assert cnpj_mod.is_valid("08.431.747/0001-06")
        assert not cnpj_mod.is_valid("08431747000107")
        # Repunits satisfy the arithmetic but are not real CNPJs.
        assert not cnpj_mod.is_valid("11111111111111")

    def test_comparison_crosses_formatting_differences(self) -> None:
        assert cnpj_mod.same("08.431.747/0001-06", "08431747000106")
        assert not cnpj_mod.same("08431747000106", "34895752000180")
        assert not cnpj_mod.same(None, "08431747000106")

    def test_masking_round_trips(self) -> None:
        assert cnpj_mod.format_masked("08431747000106") == "08.431.747/0001-06"


class TestClock:
    def test_directory_names_are_zero_padded_so_sorting_is_chronological(self) -> None:
        names = [to_dir_name(date(2026, 8, 9)), to_dir_name(date(2026, 8, 10))]
        assert names == ["2026-08-09", "2026-08-10"]
        assert sorted(names) == names

    def test_parse_dir_name_rejects_everything_that_is_not_a_date(self) -> None:
        assert parse_dir_name("2026-08-14") == date(2026, 8, 14)
        for name in ("_inbox", ".tmp", "14-08-2026", "2026-8-4", "notes"):
            assert parse_dir_name(name) is None

    def test_delivery_is_stamped_with_the_source_timezone_not_the_host(self) -> None:
        parsed = parse_delivery("13/08/2026 19:34")
        assert parsed == datetime(2026, 8, 13, 19, 34, tzinfo=source_tz())
        assert parsed.tzinfo is source_tz()

    def test_delivery_without_a_time_still_parses(self) -> None:
        assert parse_delivery("13/08/2026").date() == date(2026, 8, 13)

    def test_an_unparseable_delivery_is_a_contract_error(self) -> None:
        # The whole archive is keyed on this field, so it cannot degrade quietly.
        with pytest.raises(SourceContractError):
            parse_delivery("2026-08-13")

    @pytest.mark.parametrize(
        ("raw", "fmt", "expected"),
        [
            ("07/2026", "2", "2026-07"),  # month competence
            ("11/08/2026", "3", "2026-08-11"),  # date
            ("14/09/2026 23:59", "4", "2026-09-14T23:59"),  # date with time
            ("14/09/2026", "4", "2026-09-14"),  # declared 4, arrived as 3
            ("", "3", None),
            (None, None, None),
        ],
    )
    def test_all_three_reference_formats(
        self, raw: str | None, fmt: str | None, expected: str | None
    ) -> None:
        assert parse_reference(raw, fmt) == expected

    def test_an_unrecognised_reference_degrades_instead_of_failing(self) -> None:
        # Unlike dataEntrega this is metadata, so a surprise must not drop the row.
        assert parse_reference("sometime", "3") == "sometime"

    def test_reference_dates_may_legitimately_be_in_the_future(self) -> None:
        assert parse_reference("14/09/2099", "3") == "2099-09-14"


class TestRetentionWindow:
    def test_n_counts_dates_including_today(self) -> None:
        window = retention_window(7, reference=date(2026, 8, 14))
        assert window.first == date(2026, 8, 8)
        assert window.last == date(2026, 8, 14)
        assert len(window.dates()) == 7

    def test_n_of_one_keeps_only_today(self) -> None:
        window = retention_window(1, reference=date(2026, 8, 14))
        assert window.first == window.last == date(2026, 8, 14)

    def test_the_window_spans_month_and_year_boundaries(self) -> None:
        assert retention_window(7, reference=date(2026, 1, 3)).first == date(2025, 12, 28)
        assert retention_window(3, reference=date(2026, 3, 1)).first == date(2026, 2, 27)

    def test_membership_by_date_and_by_stored_string_agree(self) -> None:
        window = retention_window(7, reference=date(2026, 8, 14))
        assert window.contains(date(2026, 8, 8))
        assert not window.contains(date(2026, 8, 7))
        # The string form is only sound because the format is zero-padded.
        assert window.contains_str("2026-08-08")
        assert not window.contains_str("2026-08-07")

    def test_zero_or_negative_retention_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            retention_window(0)


class TestConfigurableTimezone:
    """The zone is settable, but it is installed once and read through a call."""

    @pytest.fixture(autouse=True)
    def restore_default(self):
        # The zone is process-wide state, so a test that changes it has to put
        # it back or every later test dates its fixtures differently.
        yield
        set_timezone(DEFAULT_TIMEZONE)

    def test_the_default_is_the_source_timezone(self) -> None:
        assert DEFAULT_TIMEZONE == "America/Sao_Paulo"
        assert str(source_tz()) == DEFAULT_TIMEZONE

    def test_setting_it_changes_how_delivery_is_stamped(self) -> None:
        set_timezone("UTC")
        assert str(source_tz()) == "UTC"
        parsed = parse_delivery("13/08/2026 19:34")
        assert parsed.utcoffset().total_seconds() == 0

    def test_an_unknown_zone_refuses_to_start_rather_than_falling_back(self) -> None:
        # Filing an archive under the wrong dates is worse than not starting.
        with pytest.raises(ConfigError, match="not an IANA timezone"):
            set_timezone("America/Atlantis")
        assert str(source_tz()) == DEFAULT_TIMEZONE
