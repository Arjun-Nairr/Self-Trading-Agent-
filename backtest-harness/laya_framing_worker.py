"""
laya_framing_worker.py -- EXPERIMENT-ONLY worker for the Laya input-framing /
answer-label experiment (docs/evaluations/laya_framing_plan_2026-10-04.md).

Runs inside the UNCHANGED production image (hermes-laya-worker:2), mounted
read-only at /exp with the same isolation flags as the production worker
(laya_framing.docker_command). The production worker, image tag and selector
path are not touched.

It loads the pinned checkpoint once through laya_adapter.LayaScorer.load (exact
package version, pinned commit, weight SHA-256), denies Python-level sockets,
then answers JSON lines over stdin/stdout:

  -> {"id": N, "questions": {<one noul question>}, "states": [<dict | str>, ...]}
  <- {"id": N, "records": [{"p", "answer_confidence", "input_tokens", "state_tokens",
                            "state_tokens_dropped", "truncated"}, ...],
      "options": {"rendered": [...], "tokens_uncapped": [...], "kept": [...], "head_tokens": int}}
  <- {"id": N, "error": "..."}

The question must be laya_adapter's production question and criteria; only the
optional noul `labels` may differ (checked here, and again by the controller).
"""
from __future__ import annotations

import json
import os
import sys

BATCH_SIZE = 32   # = laya_adapter.BATCH_SIZE


def _check_question(questions, adapter) -> str:
    if not isinstance(questions, dict) or set(questions) != {adapter.ANSWER_FIELD}:
        raise ValueError("exactly one question, keyed by the production answer field")
    q = questions[adapter.ANSWER_FIELD]
    base = adapter.REVIEW_QUESTIONS[adapter.ANSWER_FIELD]
    if {k: v for k, v in q.items() if k != "labels"} != base:
        raise ValueError("question type, instructions and criteria must equal the production question")
    return adapter.ANSWER_FIELD


def _option_report(tok, q) -> dict:
    from laya.common import build_head, encode_text, render_options
    internal = {"t": "noul", "ins": q["instructions"], "crit": dict(q["criteria"])}
    if "labels" in q:
        internal["labels"] = q["labels"]
    opts = render_options(internal)
    full = [len(encode_text(tok, " " + o, add_special_tokens=False)["input_ids"]) for o in opts]
    kept = [tok.decode(encode_text(tok, " " + o, add_special_tokens=False, truncation=True,
                                   max_length=48)["input_ids"]).strip() for o in opts]
    ids, _, stats = build_head(tok, internal, 192)
    return {"rendered": opts, "tokens_uncapped": full, "kept": kept, "head_tokens": len(ids),
            "options_distinct": stats["options_distinct"]}


def main(argv: list[str]) -> int:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    proto_out, sys.stdout = sys.stdout, sys.stderr
    sys.path.insert(0, "/app")
    import laya
    import laya_adapter
    from laya_worker import _deny_network

    scorer = laya_adapter.LayaScorer.load(argv[1])
    _deny_network()
    meta = scorer.metadata
    proto_out.write(json.dumps({"event": "ready", "package_version": meta.package_version,
                                "revision": meta.revision, "question": meta.question}) + "\n")
    proto_out.flush()
    tok = scorer._agent.tok
    for line in sys.stdin:
        if not line.strip():
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req["id"]
            field = _check_question(req["questions"], laya_adapter)
            states = req["states"]
            if not isinstance(states, list) or not all(isinstance(s, (dict, str)) for s in states):
                raise ValueError("states must be a list of dicts or strings")
            res = laya.decide_batch(scorer._agent, states, questions=req["questions"],
                                    return_details=True, batch_size=BATCH_SIZE)
            records = []
            for r in res:
                ans = r.answers[field]
                u = r.usage or {}
                records.append({"p": float(ans["noul"]), "answer_confidence": r.answer_confidence[field],
                                "input_tokens": u.get("input_tokens"), "state_tokens": u.get("state_tokens"),
                                "state_tokens_dropped": u.get("state_tokens_dropped"),
                                "truncated": u.get("truncated")})
            reply = {"id": req_id, "records": records, "options": _option_report(tok, req["questions"][field])}
        except Exception as exc:
            reply = {"id": req_id, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        proto_out.write(json.dumps(reply) + "\n")
        proto_out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
