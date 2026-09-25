"""2026-09-25 incident: Kavi asked "close or drop?" about two old tasks,
Megha said "close them", and Kavi closed three other tasks. A close while a
stale-task offer is pending may touch only the offered tasks (or a task the
message names); drop on the offer = close; the offered tasks are always
loaded; the open-task fetch pages past 100. Fictional task titles."""

from capabilities.kavi_persona.reply_intent_parser import (
    _apply_deterministic_rules, _names_task,
)
from kavi_runtime.runtime.imessage_dispatch import _ensure_question_tasks

OFFER = [
    {"id": "q-a", "task_id": "t-tickets", "task_title_rendered": "MJ Decide on season ticket group buy",
     "source_subject": "Stale-task close-or-drop nudge"},
    {"id": "q-b", "task_id": "t-gift", "task_title_rendered": "MJ Send Max gift options",
     "source_subject": "Stale-task close-or-drop nudge"},
]
SURVEY = {"id": "t-survey", "title": "MJ Complete 360 feedback survey for Dana (by Fri)"}
RSVP = {"id": "t-rsvp", "title": "MJ RSVP for investor meeting"}


def _close(*targets, text="Close them"):
    return [{"type": "close_task", "target_text": text, "targets": list(targets), "confidence": "high"}]


def test_close_on_unoffered_tasks_becomes_clarify_with_offer_as_candidates():
    out = _apply_deterministic_rules(_close(SURVEY, RSVP), sender="megha",
                                     pending_questions=OFFER, inbound_text="Close them")
    assert [i["type"] for i in out] == ["clarify"]
    assert {t["id"] for t in out[0]["targets"]} == {"t-tickets", "t-gift"}


def test_close_on_offered_tasks_passes_through():
    targets = [{"id": "t-tickets", "title": OFFER[0]["task_title_rendered"]},
               {"id": "t-gift", "title": OFFER[1]["task_title_rendered"]}]
    out = _apply_deterministic_rules(_close(*targets), sender="megha",
                                     pending_questions=OFFER, inbound_text="Close them")
    assert out[0]["type"] == "close_task" and {t["id"] for t in out[0]["targets"]} == {"t-tickets", "t-gift"}


def test_named_task_outside_offer_still_closes():
    out = _apply_deterministic_rules(_close(SURVEY, text="close the survey"), sender="megha",
                                     pending_questions=OFFER, inbound_text="close the 360 survey")
    assert out[0]["type"] == "close_task" and out[0]["targets"][0]["id"] == "t-survey"


def test_drop_on_offer_is_a_close_of_the_offered_tasks():
    intents = [{"type": "qa_drop", "target_text": "drop them", "confidence": "high",
                "targets": [{"id": "q-a", "title": ""}, {"id": "q-b", "title": ""}]}]
    out = _apply_deterministic_rules(intents, sender="megha", pending_questions=OFFER,
                                     inbound_text="drop them")
    assert [i["type"] for i in out] == ["close_task"]
    assert {t["id"] for t in out[0]["targets"]} == {"t-tickets", "t-gift"}


def test_no_offer_pending_leaves_closes_alone():
    out = _apply_deterministic_rules(_close(SURVEY, RSVP), sender="megha",
                                     pending_questions=[], inbound_text="Close them")
    assert out[0]["type"] == "close_task"


def test_names_task_ignores_filler_words():
    assert not _names_task("Close them", SURVEY["title"])
    assert _names_task("the survey is done", SURVEY["title"])


class _Graph:
    def __init__(self, tasks):
        self.tasks = tasks

    def get_todo_task(self, list_id, task_id):
        return self.tasks[task_id]


def test_offered_tasks_are_loaded_even_when_fetch_missed_them():
    graph = _Graph({"t-tickets": {"id": "t-tickets", "title": "x", "status": "notStarted"},
                    "t-gift": {"id": "t-gift", "title": "y", "status": "completed"}})
    out = _ensure_question_tasks(graph, "L", [SURVEY], OFFER)
    assert [t["id"] for t in out] == ["t-survey", "t-tickets"]  # completed one not added


def test_open_task_fetch_follows_pages(monkeypatch):
    from kavi_runtime import graph_client as gc

    pages = [{"value": [{"id": f"a{i}"} for i in range(100)], "@odata.nextLink": "next"},
             {"value": [{"id": "b0"}, {"id": "b1"}]}]
    calls = []

    class R:
        def __init__(self, body):
            self.body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self.body

    def fake_get(url, **kw):
        calls.append(url)
        return R(pages[len(calls) - 1])

    monkeypatch.setattr(gc.httpx, "get", fake_get)
    client = gc.GraphClient.__new__(gc.GraphClient)
    client._headers = lambda account=None: {}
    assert len(client.list_open_todo_tasks("L", top=400)) == 102
    assert calls[1] == "next"
