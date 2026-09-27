from src.etl.web_extract import find_terms_link, has_password_form, jsonld_events, main_text, robots_meta, site_name
from tests.web_fakes import fixture


def test_main_text_skips_navigation_and_footer():
    text = main_text(fixture("venue_text.html"))
    assert "pancake breakfast" in text and "Home | About" not in text


def test_main_text_falls_back_to_body_when_main_is_only_a_banner():
    html = "<html><body><main>Cookies ahead! I understand</main><section>" + "OCT 03 Girls and STEAM Showcase. " * 30 + "</section></body></html>"
    assert "Girls and STEAM" in main_text(html)


def test_jsonld_graph_and_subtypes_found():
    assert [e["name"] for e in jsonld_events(fixture("venue_jsonld.html"))] == ["Toddler Science Morning", "Online Webinar"]


def test_page_metadata_helpers():
    assert site_name(fixture("venue_jsonld.html")) == "Harbour Science Centre"
    assert "noindex" in robots_meta(fixture("noindex.html"))
    assert has_password_form(fixture("login_wall.html"))
    assert find_terms_link(fixture("venue_jsonld.html"), "https://h.example/events") == "https://h.example/terms-of-use"


def test_page_wrapped_in_a_form_keeps_its_content():
    html = "<html><body><form id='aspnetForm'><input name='q'><button>Go</button><main>" + "Oct 3 Family skate at the rink. " * 30 + "</main></form></body></html>"
    text = main_text(html)
    assert "Family skate" in text and "Go" not in text.split()
