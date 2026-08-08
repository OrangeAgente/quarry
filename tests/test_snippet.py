"""Library card previews must read as prose, not as markdown source."""
from markdown_render import snippet


def test_strips_link_and_image_syntax():
    raw = ("[![geeksforgeeks](https://media.geeksforgeeks.org/gfg-gg-logo.svg)]"
           "(https://www.geeksforgeeks.org/) The water cycle describes how water "
           "moves through the atmosphere, land and oceans.")
    out = snippet(raw)
    assert "https://" not in out and "![" not in out and "](" not in out
    assert "water cycle describes" in out


def test_skips_leading_site_chrome():
    raw = ("[Jump to content](https://en.wikipedia.org/wiki/Eclipse#bodyContent)\n"
           "Main menu\n"
           "move to sidebar\n"
           "hide\n"
           "An eclipse occurs when one astronomical body blocks light from another.")
    out = snippet(raw)
    assert out.startswith("An eclipse occurs")
    assert "sidebar" not in out and "Main menu" not in out


def test_truncates_on_a_word_boundary():
    out = snippet("alpha bravo charlie delta echo foxtrot golf hotel " * 20, limit=60)
    assert len(out) <= 61          # limit plus the ellipsis
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")   # no dangling space before the ellipsis
    assert " ".join(out[:-1].split()) == out[:-1]  # no partial word left hanging


def test_empty_and_chrome_only_input():
    assert snippet("") == ""
    assert snippet(None) == ""
    # A page that is nothing but navigation yields nothing rather than noise.
    assert snippet("Jump to content\nMain menu\nhide\n") == ""


def test_collapses_whitespace():
    out = snippet("Real   content\n\n\nspread   over\tlines.")
    assert out == "Real content spread over lines."


def test_starts_at_prose_not_at_a_navigation_menu():
    """The real failure mode: Wikipedia-style nav is a list of link labels, so
    a preview taken from the top is identical on every page from that site."""
    raw = ("[Jump to content](https://en.wikipedia.org/wiki/Solar_power#bodyContent)\n"
           "Main menu\nmove to sidebar hide\nNavigation\n"
           "  * [Main page](https://en.wikipedia.org/wiki/Main_Page)\n"
           "  * [Contents](https://en.wikipedia.org/wiki/Wikipedia:Contents)\n"
           "  * [Current events](https://en.wikipedia.org/wiki/Portal:Current_events)\n"
           "Solar power is the conversion of energy from sunlight into electricity, "
           "either directly using photovoltaics or indirectly using concentrated solar power.")
    out = snippet(raw)
    assert out.startswith("Solar power is the conversion")
    for chrome in ("Main page", "Current events", "Jump to content"):
        assert chrome not in out


def test_sentence_shaped_site_furniture_is_skipped():
    """Some chrome is a valid sentence and passes a naive prose test — it is
    site-wide, so it would appear on every card from that domain."""
    raw = ("The content is as wide as possible for your browser window. Color "
           "Automatic Light Dark This page is always in light mode.\n"
           "Photovoltaic cells convert light directly into electricity using "
           "semiconducting materials that exhibit the photovoltaic effect.")
    out = snippet(raw)
    assert out.startswith("Photovoltaic cells convert")


def test_adjacent_links_do_not_fuse():
    out = snippet("[Topics](/a)[Concepts](/b) " + "and then a real sentence follows here. " * 3)
    assert "TopicsConcepts" not in out


def test_emphasis_markers_and_escapes_removed():
    raw = ("**theory of solar cells** explains how photons become current, and "
           "it was reviewed on _17 April 2026_ per the \\(cited\\) source.")
    out = snippet(raw)
    assert "**" not in out and "_17" not in out
    assert "\\(" not in out and "\\)" not in out
    assert "theory of solar cells" in out and "17 April 2026" in out


def test_prefers_readable_text_over_reference_defs():
    raw = "[1]: https://example.com/a\n[2]: https://example.com/b\nActual prose here."
    out = snippet(raw)
    assert "example.com" not in out
    assert "Actual prose here." in out
