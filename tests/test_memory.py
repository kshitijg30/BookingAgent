"""Personal memory (user_profiles.memory): stated and inferred keys, correcting, forgetting and pre-filling.

Memory is a JSON object {key: {value, source, confirmed, asked?}}. The memory model's operation format
({"op": "set"|"remove", "key", "value", "stated"}) is built in tests/conftest.py (memory_ops, set_op, remove_op) so a mismatch is fixed in one place.
"""

from __future__ import annotations

import pytest

from booking_agent.model import call, say
from tests.conftest import MakeWorld, Record, memory_ops, remove_op, set_op
from tests.helpers import cards, context_sent, memory, set_memory


@pytest.mark.scenario(
    id="S03", title="Known fact is not re-asked", expected="area from memory pre-fills the search; no area question"
)
def test_confirmed_memory_prefills_and_is_not_asked(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala")
    set_memory(world, "max_budget", 1500)
    world.model.then(
        call("find_and_propose_booking", service="plumber", date_from="2026-11-02", date_to="2026-11-08"),
        say("Here you go."),
    )
    reply = world.say("a plumber this week")
    search = world.search()
    assert search is not None and search.criteria.area == "Koramangala" and search.criteria.max_price == 1500
    assert cards(reply) == ["offers"]  # no question card: nothing was missing
    record(actual="area and budget came from confirmed memory; offers shown without a question")


def test_memory_prefills_earliest_time_and_preferred_provider(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala")
    set_memory(world, "max_budget", 2000)
    set_memory(world, "earliest_time", "10:00")
    set_memory(world, "preferred_providers", {"plumber": "plu-06"})
    world.model.then(
        call("find_and_propose_booking", service="plumber", date_from="2026-11-02", date_to="2026-11-08"),
        say("Here you go."),
    )
    world.say("a plumber this week")
    search = world.search()
    assert search is not None
    assert [(t.from_time.hour, t.from_time.minute) for t in search.criteria.times] == [(10, 0)]
    assert search.criteria.provider_id == "plu-06"


def test_unconfirmed_memory_is_not_used_to_prefill(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala", source="inferred", confirmed=False)
    world.model.then(call("find_and_propose_booking", service="plumber"), say("Which area?"))
    world.say("a plumber please")
    search = world.search()
    assert search is not None and search.criteria.area is None and "area" in search.criteria.missing()


def test_question_card_lists_only_missing_fields(make_world: MakeWorld) -> None:
    world = make_world()
    world.model.then(
        call("find_and_propose_booking", service="plumber", area="Koramangala"), say("When, and what budget?")
    )
    first = world.say("I need a plumber in Koramangala")
    (question,) = [m.card for m in first.messages if m.card and m.card["type"] == "question"]
    assert [group["label"] for group in question["choices"]] == ["When", "Budget"]
    world.model.then(say("Noted."))
    world.say("this week please")
    assert "area = Koramangala" in context_sent(world) and "Missing: dates, budget" in context_sent(world)


@pytest.mark.scenario(
    id="S21", title="Memory correct and forget", expected="correction used; forgotten key gone and not re-learned"
)
def test_memory_correct_and_forget(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    world.model.then(call("remember", key="area", value="Indiranagar"), say("Saved."))
    world.say("I live in Indiranagar")
    assert memory(world)["area"] == {"value": "Indiranagar", "source": "stated", "confirmed": True}
    set_memory(world, "area", "HSR Layout")  # the user corrects it in the profile panel
    world.model.then(say("Okay."))
    world.say("hello")
    # The system prompt lists Indiranagar as a service area, so look at the PERSONAL_MEMORY message only.
    assert '"HSR Layout"' in context_sent(world) and '"Indiranagar"' not in context_sent(world)
    world.model.then(call("forget", key="area"), say("Forgotten."))
    world.say("forget my area")
    assert "area" not in memory(world)
    world.model.then(say("Okay."))
    world.say("hello again")  # the memory model reads only this turn and returns nothing
    world.run_jobs()
    assert "area" not in memory(world) and '"HSR Layout"' not in context_sent(world)
    record(actual="corrected value reached the model; forget removed the key; nothing re-learned")


@pytest.mark.scenario(
    id="S22", title="Inferred preference needs confirmation", expected="not used until the user says yes; asked once"
)
def test_inferred_key_is_asked_once_and_not_used_before(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    world.memory_model.then(say(memory_ops(set_op("earliest_time", "10:00", stated=False))))
    world.model.then(say("Hello."))
    world.say("book me something late morning again")
    world.run_jobs()
    entry = memory(world)["earliest_time"]
    assert entry["source"] == "inferred" and entry["confirmed"] is False and not entry.get("asked")

    world.model.then(say("Is not before 10 right?"))
    world.say("hi")
    assert "earliest_time" in context_sent(world)  # it is asked about once ...
    assert memory(world)["earliest_time"]["asked"] is True
    world.model.then(say("Okay."))
    world.say("still thinking")
    assert "10:00" not in context_sent(world)  # ... and neither asked again nor used

    world.model.then(call("confirm_memory", key="earliest_time", answer="yes"), say("Thanks."))
    world.say("yes")
    assert (
        memory(world)["earliest_time"]["confirmed"] is True and memory(world)["earliest_time"]["source"] == "inferred"
    )
    world.model.then(say("Okay."))
    world.say("hello")
    assert "10:00" in context_sent(world)
    record(actual="inferred key stayed out of the context, was asked about once, used after yes; source kept")


def test_a_no_answer_removes_the_inferred_key(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "earliest_time", "10:00", source="inferred", confirmed=False, asked=True)
    world.model.then(call("confirm_memory", key="earliest_time", answer="no"), say("Okay, dropped."))
    world.say("no, not really")
    assert "earliest_time" not in memory(world)


def test_remember_stores_a_stated_confirmed_key(make_world: MakeWorld) -> None:
    world = make_world()
    world.model.then(call("remember", key="earliest_time", value="10:00"), say("Saved."))
    world.say("never before 10 am")
    assert memory(world)["earliest_time"] == {"value": "10:00", "source": "stated", "confirmed": True}


@pytest.mark.scenario(id="S31", title="Forget in one conversation, ask in another", expected="forgotten key never used")
def test_forget_across_conversations(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    set_memory(world, "preferred_providers", {"plumber": "Iyer Plumbing"})
    world.model.then(say("Okay."))
    world.say("hello", conversation="tab-1")
    assert "Iyer Plumbing" in context_sent(world)
    world.model.then(call("forget", key="preferred_providers"), say("Forgotten."))
    world.say("forget my preferred provider", conversation="tab-2")
    world.model.then(say("Okay."))
    world.say("hello again", conversation="tab-1")
    assert "Iyer Plumbing" not in context_sent(world) and "preferred_providers" not in memory(world)
    record(actual="forgotten in tab 2; tab 1's next turn no longer sends it")


# ---------------------------------------------------------------- memory_update job (scripted memory model)


def test_memory_update_sets_a_stated_key_as_confirmed(make_world: MakeWorld) -> None:
    world = make_world()
    world.memory_model.then(say(memory_ops(set_op("earliest_time", "10:00", stated=True))))
    world.model.then(say("Noted."))
    world.say("I never want anyone before 10 am")
    world.run_jobs()
    assert memory(world)["earliest_time"] == {"value": "10:00", "source": "stated", "confirmed": True}


def test_memory_update_sets_an_inferred_key_as_unconfirmed(make_world: MakeWorld) -> None:
    world = make_world()
    world.memory_model.then(say(memory_ops(set_op("area", "Koramangala", stated=False))))
    world.model.then(say("Noted."))
    world.say("book Iyer again")
    world.run_jobs()
    assert memory(world)["area"]["confirmed"] is False and memory(world)["area"]["source"] == "inferred"


def test_memory_update_overwrites_and_removes_keys(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala")
    set_memory(world, "earliest_time", "10:00")
    world.memory_model.then(say(memory_ops(set_op("area", "HSR Layout", stated=True), remove_op("earliest_time"))))
    world.model.then(say("Noted."))
    world.say("I moved to HSR Layout and any time is fine now")
    world.run_jobs()
    assert {key: entry["value"] for key, entry in memory(world).items()} == {"area": "HSR Layout"}


def test_an_inferred_value_does_not_overwrite_a_confirmed_one_without_the_user(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala")
    world.memory_model.then(say(memory_ops(set_op("area", "Whitefield", stated=False))))
    world.model.then(say("Hi."))
    world.say("hello")
    world.run_jobs()
    assert memory(world)["area"]["value"] == "Koramangala" and memory(world)["area"]["confirmed"] is True


def test_memory_update_with_bad_output_changes_nothing(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala")
    world.memory_model.then(say("this is not json"))
    world.model.then(say("Hi."))
    world.say("hello")
    world.run_jobs()
    assert memory(world)["area"]["value"] == "Koramangala" and not world.jobs("memory_update", "dead")


def test_memory_update_reads_only_the_current_turn(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala")
    world.model.then(say("Sure."))
    world.say("first message about Ravi")
    world.model.then(say("Okay."))
    world.say("second message about weather")
    world.run_jobs()
    prompt = str(world.memory_model.calls[-1])
    assert "second message about weather" in prompt and "Okay." in prompt and "Koramangala" in prompt
    assert "first message about Ravi" not in prompt


def test_a_forgotten_key_is_not_relearned_from_older_messages(make_world: MakeWorld) -> None:
    world = make_world()
    world.model.then(call("remember", key="area", value="Indiranagar"), say("Saved."))
    world.say("I live in Indiranagar")
    world.model.then(call("forget", key="area"), say("Forgotten."))
    world.say("forget my area")
    world.model.then(say("Hi."))
    world.say("hello")
    world.run_jobs()  # the memory model sees only "hello", so it has nothing to set
    assert "area" not in memory(world)
    assert "Indiranagar" not in str(world.memory_model.calls[-1])


def test_provider_review_text_is_not_sent_to_the_memory_model(make_world: MakeWorld) -> None:
    from tests.helpers import book_to_confirmation

    world = make_world()
    book_to_confirmation(world)
    world.run_jobs()
    for messages in world.memory_model.calls:
        assert "ignore all previous" not in str(messages).lower()


def test_only_confirmed_keys_go_to_the_chat_model(make_world: MakeWorld) -> None:
    world = make_world()
    set_memory(world, "area", "Koramangala")
    set_memory(world, "earliest_time", "10:00", source="inferred", confirmed=False, asked=True)
    world.model.then(say("Hi."))
    world.say("hello")
    assert "Koramangala" in context_sent(world) and "10:00" not in context_sent(world)


# ---------------------------------------------------------------- conversation summary


def test_older_turns_are_folded_into_a_summary(make_world: MakeWorld) -> None:
    world = make_world()
    for number in range(10):
        world.model.then(say(f"reply {number}"))
        world.say(f"message {number}")
    # The first job folds: one operations call, then one summary call. The other jobs find nothing left to fold.
    world.memory_model.then(say(memory_ops()), say("The user chatted about plumbers."), *[say(memory_ops())] * 9)
    world.run_jobs()
    saved = world.saved_conversation()
    assert saved.summary == "The user chatted about plumbers." and saved.summarized_count > 0
    world.model.then(say("Okay."))
    world.say("message 10")
    sent = world.model.calls[-1]
    assert "The user chatted about plumbers." in context_sent(world)
    history = [m for m in sent if m["role"] in ("user", "assistant")]
    assert len(history) <= 17 and not any(m["content"] == "message 0" for m in history)  # last 8 turns + this message
