"""The two correlation rules that decide when a re-filing replaces a document.

`correlate` is pure, so the rules can be pinned exactly here rather than through
the pipeline. What matters most is the *negative* cases: this is the one place
in the robot that deletes a file the source never told it to delete, and the
cost of a wrong match is a document the reader silently never sees.
"""

from __future__ import annotations

from fii_docs_watcher.manifest.repo import LocalState, ManifestDocument
from fii_docs_watcher.pipeline.supersede import correlate


def doc(
    document_id: int,
    version: int = 1,
    *,
    fundosnet_id: int = 21348,
    category: str = "Relatórios",
    doc_type: str = "Relatório Gerencial",
    species: str = "",
    reference: str | None = "07/2026",
    state: str = LocalState.AVAILABLE.value,
) -> ManifestDocument:
    return ManifestDocument(
        document_id=document_id,
        version=version,
        fundosnet_id=fundosnet_id,
        entity_cnpj="08431747000106",
        fund_description="HEDGE BRASIL SHOPPING FII",
        category=category,
        doc_type=doc_type,
        species=species,
        reference_date=reference,
        reference_date_format="2",
        delivery_date="2026-08-18",
        delivery_at="2026-08-18T09:30",
        modality="AP",
        status="Ativo com visualização",
        local_state=state,
        path=f"2026-08-18/HGBS11_Relatorios_{document_id}_V{version:02d}.pdf",
        extension="pdf",
        content_hash="deadbeef",
        downloaded_at="2026-08-18T10:00:00-03:00",
        purged_at=None,
        superseded_at=None,
        superseded_by_id=None,
        superseded_by_version=None,
        seen_at="2026-08-18T10:00:00-03:00",
    )


class TestCrossIdRule:
    def test_a_refiling_under_a_new_id_replaces_the_original(self) -> None:
        # The case this rule exists for: Fundos.NET published the correction as
        # a separate document, so publication identity alone cannot see it.
        pairs = correlate([doc(1295651, 1), doc(1295810, 2)])

        assert len(pairs) == 1
        loser, winner = pairs[0]
        assert loser.identity == (1295651, 1)
        assert winner.identity == (1295810, 2)

    def test_two_documents_at_the_same_version_never_correlate(self) -> None:
        # The guard the whole rule rests on. Two Fatos Relevantes on one day
        # share every key field and are both V01; treating them as one
        # publication would delete a document nobody ever read.
        assert correlate([doc(1295651, 1), doc(1295810, 1)]) == []

    def test_a_different_reference_date_is_a_different_publication(self) -> None:
        assert correlate([doc(1295651, 1), doc(1295810, 2, reference="06/2026")]) == []

    def test_a_different_entity_is_never_correlated(self) -> None:
        assert correlate([doc(1295651, 1), doc(1295810, 2, fundosnet_id=23240)]) == []

    def test_a_different_species_is_a_different_publication(self) -> None:
        # Assemblies of one fund on one day differ only by especieDocumento.
        pairs = correlate(
            [
                doc(1295651, 1, category="Assembleia", species="Edital de Convocação"),
                doc(1295810, 2, category="Assembleia", species="Carta Consulta"),
            ]
        )
        assert pairs == []

    def test_stray_spaces_and_case_do_not_split_a_group(self) -> None:
        # `tipoDocumento` really does arrive with trailing spaces, so comparing
        # it raw would miss the match and leave both files in the archive.
        pairs = correlate([doc(1295651, 1), doc(1295810, 2, doc_type="RELATÓRIO GERENCIAL ")])
        assert len(pairs) == 1

    def test_without_a_reference_date_the_cross_id_rule_stands_down(self) -> None:
        # The strongest discriminator in the key. Without it the key is barely
        # more than a category, so the rule declines rather than guesses.
        assert correlate([doc(1295651, 1, reference=None), doc(1295810, 2, reference=None)]) == []


class TestSameIdRule:
    def test_a_higher_version_of_the_same_id_replaces_the_lower(self) -> None:
        pairs = correlate([doc(1001, 1), doc(1001, 2)])
        assert [(loser.identity, winner.identity) for loser, winner in pairs] == [
            ((1001, 1), (1001, 2))
        ]

    def test_it_still_applies_without_a_reference_date(self) -> None:
        # Publication identity does not need the cross-id key, which is why the
        # two rules are kept separate: a re-filing may correct the date itself.
        pairs = correlate([doc(1001, 1, reference=None), doc(1001, 2, reference=None)])
        assert len(pairs) == 1

    def test_a_corrected_reference_date_is_still_caught_by_the_id(self) -> None:
        pairs = correlate([doc(1001, 1), doc(1001, 2, reference="06/2026")])
        assert len(pairs) == 1


class TestGroupsOfThree:
    def test_every_earlier_version_points_at_the_live_one(self) -> None:
        pairs = correlate([doc(1001, 1), doc(1002, 2), doc(1003, 3)])
        assert sorted((loser.identity, winner.identity) for loser, winner in pairs) == [
            ((1001, 1), (1003, 3)),
            ((1002, 2), (1003, 3)),
        ]

    def test_a_pending_document_can_lose_before_it_is_ever_downloaded(self) -> None:
        pairs = correlate(
            [doc(1295651, 1, state=LocalState.DISCOVERED.value), doc(1295810, 2)]
        )
        assert len(pairs) == 1
        assert pairs[0][0].local_state == LocalState.DISCOVERED
