"""Email judge routing (offline replay only, not wired into compose yet).
Cheap model by default; strong model for replies/forwards and named
senders; strong-model second look when the cheap model skips a subject that
usually means action. Fictional senders only."""

from capabilities.inbox_to_task.routing import CHEAP, STRONG, first_route, needs_second_look

ROUTING = {
    "strong_on_reply_or_forward": True,
    "strong_senders": ["maplestreetschool.org", "lakeviewswim"],
    "second_look_subjects": ["just messaged you", r"\bdelivered\b"],
}


def _email(sender, subject):
    return {"from_address": sender, "subject": subject}


def test_no_config_keeps_everything_on_strong_model():
    assert first_route(_email("a@b.com", "hi"), None)[0] == STRONG


def test_reply_or_forward_goes_strong():
    assert first_route(_email("a@b.com", "RE: lunch"), ROUTING) == (STRONG, "reply_or_forward")
    assert first_route(_email("a@b.com", "Fwd: form"), ROUTING)[0] == STRONG


def test_strong_sender_by_domain_subdomain_or_substring():
    assert first_route(_email("t@maplestreetschool.org", "News"), ROUTING)[0] == STRONG
    assert first_route(_email("t@mail.maplestreetschool.org", "News"), ROUTING)[0] == STRONG
    assert first_route(_email("support@lakeviewswim.zendesk.com", "Hi"), ROUTING)[0] == STRONG


def test_everything_else_goes_cheap():
    assert first_route(_email("deals@shop.com", "Sale"), ROUTING) == (CHEAP, "default")


def test_second_look_only_on_cheap_skip_with_matching_subject():
    e = _email("x@linkedin.com", "Sam just messaged you")
    assert needs_second_look(e, {"status": "skipped"}, ROUTING)
    assert not needs_second_look(e, {"status": "task"}, ROUTING)
    assert not needs_second_look(_email("x@y.com", "Weekly digest"), {"status": "skipped"}, ROUTING)
    assert not needs_second_look(e, {"status": "skipped"}, None)
