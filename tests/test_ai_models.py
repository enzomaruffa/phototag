import json

from phototag.models.ai import AIAnalysisResponse, keywords_to_write, subject_tag

OLD_RESPONSE = {
    "rating": 3,
    "existing_tags_used": ["portrait"],
    "new_tags_needed": ["graduation"],
    "description": "A graduate in front of a building.",
    "notes": None,
    "confidence": 0.8,
    "visible_date": None,
}


def parse(**extra: object) -> AIAnalysisResponse:
    return AIAnalysisResponse.model_validate({**OLD_RESPONSE, **extra})


def test_old_saved_response_without_named_subjects_still_parses():
    analysis = AIAnalysisResponse.model_validate_json(json.dumps(OLD_RESPONSE))

    assert analysis.named_subjects == []
    assert analysis.named_subject_tags() == []


def test_new_response_parses_named_subjects():
    analysis = parse(
        named_subjects=[
            {"name": "Mona Lisa", "kind": "artwork", "confidence": 0.95},
            {"name": "Eiffel Tower", "kind": "landmark", "confidence": 0.9},
        ],
    )

    assert [s.kind for s in analysis.named_subjects] == ["artwork", "landmark"]
    assert analysis.named_subject_tags() == ["mona_lisa", "eiffel_tower"]


def test_unknown_kind_and_bad_confidence_do_not_fail_the_response():
    analysis = parse(
        named_subjects=[
            {"name": "Thing", "kind": "spaceship", "confidence": 1.7},
            {"name": "Other", "kind": "Public Figure", "confidence": "n/a"},
        ],
    )

    assert analysis.named_subjects[0].kind == "other"
    assert analysis.named_subjects[0].confidence == 1.0
    assert analysis.named_subjects[1].kind == "public_figure"
    assert analysis.named_subjects[1].confidence == 0.0


def test_low_confidence_subjects_are_not_keywords():
    analysis = parse(
        named_subjects=[
            {"name": "Maybe Bridge", "kind": "landmark", "confidence": 0.5}
        ],
    )

    assert analysis.named_subject_tags() == []


def test_subject_tag_normalizes_names():
    assert subject_tag("Mona Lisa") == "mona_lisa"
    assert subject_tag("Porsche 911 (992)") == "porsche_911_992"
    assert subject_tag("  Café de Flore ") == "cafe_de_flore"
    assert subject_tag("Sagrada Família, Barcelona") == "sagrada_familia_barcelona"
    assert subject_tag("!!!") == ""


def test_keywords_skip_review_only_for_named_and_extra_tags():
    analysis = parse(
        named_subjects=[
            {"name": "Eiffel Tower", "kind": "landmark", "confidence": 0.9}
        ],
    )

    keywords = keywords_to_write(analysis, ["portrait"], ["focus_stack", "portrait"])

    # 'graduation' is still pending review, so it stays out
    assert keywords == ["portrait", "eiffel_tower", "focus_stack"]


def test_keywords_include_new_tags_once_approved():
    analysis = parse()

    assert keywords_to_write(analysis, ["portrait", "graduation"]) == [
        "portrait",
        "graduation",
    ]
