from phototag.ai.openai_service import OpenAIService, capture_context_line
from phototag.storage.exif import CaptureContext, parse_capture_context


def make_prompt(**kwargs) -> str:
    return OpenAIService(api_key="test")._create_prompt(**kwargs)


def test_prompt_includes_rounded_gps_and_date():
    ctx = CaptureContext(latitude=48.860611, longitude=2.337644, taken_on="2026-10-31")

    prompt = make_prompt(capture_ctx=ctx)

    assert "Taken near 48.8606, 2.3376 on 2026-10-31." in prompt


def test_prompt_has_no_capture_line_without_exif_context():
    assert "Taken " not in make_prompt()
    assert "Taken " not in make_prompt(capture_ctx=CaptureContext(None, None, None))


def test_capture_line_with_only_a_date():
    line = capture_context_line(CaptureContext(None, None, "2026-01-02"))

    assert "Taken on 2026-01-02." in line
    assert "near" not in line


def test_prompt_lists_existing_tags_with_reuse_rules():
    prompt = make_prompt(existing_tags=["portrait", "golden_light", "macro", "snow"])

    assert "Subjects: portrait" in prompt
    assert "Lighting: golden_light" in prompt
    assert "Composition: macro" in prompt
    assert "Other: snow" in prompt
    assert "don't create 'blurry'" in prompt


def test_prompt_without_tags_skips_the_vocabulary_section():
    assert "AVAILABLE TAGS (" not in make_prompt()


def test_prompt_keeps_the_rating_rubric_and_asks_for_named_subjects():
    prompt = make_prompt()

    assert "5: Absolute masterpiece" in prompt
    assert '"named_subjects"' in prompt
    assert "Never identify private individuals" in prompt


def test_source_context_comes_first():
    prompt = make_prompt(source_ctx="SOURCE CONTEXT: tiny camera\n")

    assert prompt.startswith("SOURCE CONTEXT: tiny camera")


def test_parse_capture_context_reads_signed_coordinates():
    ctx = parse_capture_context(
        {
            "GPSLatitude": -23.5505,
            "GPSLongitude": -46.6333,
            "DateTimeOriginal": "2026:10:31 17:03:22",
        }
    )

    assert ctx == CaptureContext(-23.5505, -46.6333, "2026-10-31")


def test_parse_capture_context_ignores_null_island_and_zero_dates():
    ctx = parse_capture_context(
        {"GPSLatitude": 0, "GPSLongitude": 0, "DateTimeOriginal": "0000:00:00 00:00:00"}
    )

    assert ctx == CaptureContext(None, None, None)


def test_subject_hint_goes_into_the_prompt():
    prompt = make_prompt(subject_hint="Rufous hornero nest")

    assert 'the subject is "Rufous hornero nest"' in prompt
    assert "PHOTOGRAPHER'S NOTE" not in make_prompt()
