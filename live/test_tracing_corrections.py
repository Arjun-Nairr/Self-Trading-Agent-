"""Correction pass (Tickets 1-3 batch): (1) "unchanged context" is asserted only after the observed content is
proven equal, and clipped copies are labelled as unverified; (2) the 64 KB attribute cap is enforced in UTF-8 bytes
on the complete encoded attribute, envelope included. Each test was run against the unfixed code first."""
import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tracing import otlp  # noqa: E402
from test_tracing import exporter, load_plugin  # noqa: E402
from test_tracing_hardening import (  # noqa: E402
    POLICY, SECRET, finish, gen_spans, post, pre, rebuild, request_input)


def run_requests(tmp_path, collector, requests):
    """Send (items, instructions) requests through the plugin; return {span name: span}, {span id: span}."""
    mod, h = load_plugin(tmp_path)
    for n, (items, instructions) in enumerate(requests, 1):
        pre(h, f"r{n}", n, items, instructions=instructions)
        post(h, f"r{n}", n)
    finish(mod, h)
    return gen_spans(collector), {s["spanId"]: s for s in collector.spans()}


def assert_rebuilt_is_what_was_sent(by_id, span, items):
    """Every message rebuilt from exported spans equals what THIS request carried (never an older version)."""
    for i, msg in rebuild(by_id, span).items():
        assert msg == otlp.sanitize(items[i], 4000), f"message {i} was substituted by other content"


# -- finding 1: unchanged-context detection ----------------------------------------------------------------------------

def test_same_length_messages_differing_only_in_the_middle_are_not_cited_as_unchanged(tmp_path, plugin_env):
    a = "A" * 600 + "buy WMG" + "Z" * 600
    b = "A" * 600 + "buy ATI" + "Z" * 600                       # same length, same first/last 256+ chars
    first = [{"role": "user", "content": "go"}, {"role": "assistant", "content": a}]
    second = [{"role": "user", "content": "go"}, {"role": "assistant", "content": b}, {"role": "user", "content": "n"}]
    gens, by_id = run_requests(tmp_path, plugin_env, [(first, POLICY), (second, POLICY)])
    two = request_input(gens["llm_call_2"])
    assert 1 in {m["index"] for m in two["messages"]}, "the changed message is exported again"
    assert two["history_changed_at_index"] == 1
    assert_rebuilt_is_what_was_sent(by_id, gens["llm_call_2"], second)


def test_instructions_sharing_their_first_500_chars_are_distinguished(tmp_path, plugin_env):
    head = "RULE: stops stay at technical levels. " * 14              # > 500 chars
    p1, p2 = head + "Max risk 5 percent.", head + "Max risk 9 percent."
    item = [{"role": "user", "content": "go"}]
    gens, _ = run_requests(tmp_path, plugin_env, [(item, p1), (item, p2)])
    two = request_input(gens["llm_call_2"])
    assert "same_as" not in two["system"] and two["system"]["content"] == p2


def test_truly_unchanged_content_is_still_cited_not_exported_again(tmp_path, plugin_env):
    items = [{"role": "user", "content": "go " + "x" * 3000}, {"role": "assistant", "content": "y" * 3000}]
    gens, by_id = run_requests(tmp_path, plugin_env, [(items, POLICY), (items + [{"role": "user", "content": "n"}],
                                                                          POLICY)])
    two = request_input(gens["llm_call_2"])
    assert [m["index"] for m in two["messages"]] == [2]
    assert two["inherited"] == [{"indexes": [0, 1], "recorded_in": "llm_call_1",
                                 "span_id": gens["llm_call_1"]["spanId"]}]
    assert two["system"] == {"same_as": "llm_call_1", "span_id": gens["llm_call_1"]["spanId"]}
    assert two["complete"] is True and "history_changed_at_index" not in two


def test_an_upstream_clipped_copy_is_labelled_unverified_never_claimed_identical(tmp_path, plugin_env):
    full = "RULE: stops stay at technical levels. " * 300
    clip8k = full[:8000] + "...[truncated %d chars]" % (len(full) - 8000)
    clip1k = full[:1000] + "...[truncated %d chars]" % (len(full) - 1000)
    item = lambda k: [{"role": "user", "content": "go"}] * k  # noqa: E731
    gens, _ = run_requests(tmp_path, plugin_env, [(item(1), clip8k), (item(2), clip1k), (item(3), clip8k)])
    one, two, three = (request_input(gens[f"llm_call_{i}"]) for i in (1, 2, 3))
    assert one["system"]["clipped"] is True and one["complete"] is False
    assert "same_as" not in two["system"], "a shorter clipped copy is not proof of identical instructions"
    assert two["system"]["prefix_of"] == "llm_call_1" and two["system"]["observed_chars"] == 1000
    assert "unverified" in two["system"]["equality"] and two["complete"] is False
    assert three["system"]["same_as"] == "llm_call_1", "the identical observed copy is cited"
    assert "unverified" in three["system"]["equality"] and three["complete"] is False


@pytest.mark.parametrize("seed", range(5))
def test_reconstruction_never_substitutes_old_content(tmp_path, plugin_env, seed):
    import random
    rnd = random.Random(seed)
    items = [{"role": "user", "content": "task " + "t" * 800}]
    requests = []
    for _ in range(6):
        op = rnd.choice(["append", "mutate_middle", "append", "drop"])
        if op == "mutate_middle" and len(items) > 1:
            i = rnd.randrange(len(items))
            c = items[i]["content"]
            mid = len(c) // 2
            items[i] = {**items[i], "content": c[:mid] + rnd.choice("QRS") + c[mid + 1:]}   # same length
        elif op == "drop" and len(items) > 2:
            del items[rnd.randrange(1, len(items) - 1)]
        else:
            items.append({"role": rnd.choice(["user", "assistant"]), "content": "m" * rnd.randrange(600, 900)})
        requests.append(([dict(m) for m in items], POLICY))
    gens, by_id = run_requests(tmp_path, plugin_env, requests)
    for n, (sent, _) in enumerate(requests, 1):
        assert_rebuilt_is_what_was_sent(by_id, gens[f"llm_call_{n}"], sent)


def test_equality_checks_stay_bounded_and_oversized_items_are_marked_unverified(tmp_path, plugin_env):
    mod, h = load_plugin(tmp_path)
    huge = {"role": "tool", "content": "q" * 5_000_000}
    history = [{"role": "user", "content": "w" * 4000} for _ in range(300)]
    t0 = time.perf_counter()
    for n in (1, 2):
        pre(h, f"r{n}", n, history + [huge] + [{"role": "user", "content": "n"}] * n, instructions=POLICY)
        post(h, f"r{n}", n)
    assert time.perf_counter() - t0 < 3.0, "two requests with 6 MB of history stay fast"
    finish(mod, h)
    two = request_input(gen_spans(plugin_env)["llm_call_2"])
    cited = {i for r in two["inherited"] for i in range(r["indexes"][0], r["indexes"][1] + 1)}
    assert 300 not in cited, "an item too large to compare is never cited as unchanged"
    assert 300 in two["equality_unverified"] and two["complete"] is False


# -- finding 2: the attribute byte cap ---------------------------------------------------------------------------------

PAYLOADS = {
    "ascii": ["a" * 8000] * 20,
    "emoji": ["\U0001F600" * 8000] * 5,
    "cjk": ["漢" * 8000] * 5,
    "combining": ["é" * 4000] * 10,
    "escape heavy": ['"\\\n' * 2666] * 12,
    "control chars": ["\x01" * 8000] * 3,
    "nested": {"a": [{"b": {"c": ["\U0001F600漢\"" * 2000] * 4}}] * 6},
}


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_oversized_attributes_stay_within_the_byte_cap_as_valid_marked_json(name):
    out = otlp.encode_attr(PAYLOADS[name])
    data = out.encode("utf-8")                                     # strict: no lone surrogates
    assert len(data) <= otlp.MAX_ATTR_BYTES, f"{len(data)} bytes"
    doc = json.loads(out)
    assert doc["truncated"] is True and doc["original_bytes"] > otlp.MAX_ATTR_BYTES and doc["preview"]


@pytest.mark.parametrize("extra", [-1, 0, 1, 2])
@pytest.mark.parametrize("tail", ["", "\U0001F600", "漢", '"'])
def test_values_near_the_boundary(extra, tail):
    tail_bytes = len(json.dumps(tail, ensure_ascii=False).encode()) - 2
    body = "a" * (otlp.MAX_ATTR_BYTES - 2 - tail_bytes + extra) + tail
    out = otlp.encode_attr(body, limit=10 ** 6)
    assert len(out.encode("utf-8")) <= otlp.MAX_ATTR_BYTES
    if extra <= 0:
        assert json.loads(out) == body, "a value at or under the cap is exported unchanged"
    else:
        assert json.loads(out)["truncated"] is True


def test_secrets_stay_absent_from_complete_exported_payloads_after_byte_truncation(collector):
    value = {"rows": [f"password={SECRET} " + "\U0001F600" * 7000 for _ in range(6)],
             "creds": json.dumps({"client_secret": SECRET}), "apiKey": SECRET}
    e = exporter(collector.url)
    tid = otlp.new_trace_id()
    e.submit(otlp.span_record(tid, otlp.new_span_id(), None, "big", time.time_ns(), time.time_ns() + 5,
                              otlp.observation_attrs("span", input=value, output=value)))
    assert e.flush(5)
    body = b"".join(collector.bodies)
    assert SECRET.encode() not in body
    attrs = [a for s in collector.spans() for a in s["a"].values() if isinstance(a, str)]
    assert attrs and all(len(a.encode("utf-8")) <= otlp.MAX_ATTR_BYTES for a in attrs)
